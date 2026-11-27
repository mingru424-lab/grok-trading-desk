"""One public-quote cycle: validate, decide, maybe simulate, always record.

The state file is the book of record. It is written before the fill line in
the log, so a crash between the two cannot produce a second simulated fill
on the next run. A quote that fails validation does not touch the state file.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..shared.log import EventLog
from .broker import exposure_usd, mark_position, simulate_buy, simulate_sell_all
from .config import BitgetPaperConfigError, BitgetPaperSettings, settings_from_root
from .strategy import decide_cycle
from .market import PublicMarketClient
from .models import (
    ASSUMPTION_NOTE,
    DemoDecision,
    Assumptions,
    BrokerRefusal,
    ContractError,
    FillEconomics,
    PaperFillRecord,
    PaperState,
    PnlSnapshot,
    PositionSnapshot,
    QuoteRejected,
    QuoteSnapshot,
    SourceInfo,
    StateError,
    ValidatedQuote,
    epoch_ms,
    money_str,
)
from .risk import PaperRiskGate
from .store import (
    BookLock,
    append_jsonl,
    drawdown_ratio,
    equity_of,
    lock_path_for,
    read_control,
    remember_mark,
)


@dataclass
class CycleResult:
    status: str
    reason: str
    fill_written: bool
    exit_code: int
    summary: dict[str, Any]


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise StateError("naive_clock")
    return now.astimezone(timezone.utc)


def fresh_state(settings: BitgetPaperSettings, now: datetime) -> PaperState:
    return PaperState(
        version=2,
        label="LOCAL_PAPER_DEMO",
        symbol="BTCUSDT",
        category="SPOT",
        cash_usd=settings.demo_cash_usd,
        quantity_btc=Decimal("0"),
        cost_basis_usd=Decimal("0"),
        realized_pnl_usd=Decimal("0"),
        fees_paid_usd=Decimal("0"),
        fill_count=0,
        last_mark_price=None,
        last_unrealized_pnl_usd=Decimal("0"),
        peak_equity_usd=settings.demo_cash_usd,
        updated_at=now,
    )


def load_state(path: Path, settings: BitgetPaperSettings, now: datetime) -> PaperState:
    if not path.exists():
        return fresh_state(settings, now)
    from .store import load_versioned_state

    state, migrated = load_versioned_state(path)
    if state.symbol != settings.symbol or state.category != settings.category:
        raise StateError("state_symbol_mismatch")
    if migrated:
        save_state(path, state)
    return state


def save_state(path: Path, state: PaperState) -> None:
    payload = json.dumps(json.loads(state.model_dump_json()), indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        raise StateError("state_write_failed") from None


def _plain_detail(detail: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in detail.items():
        name = str(key)[:40]
        if isinstance(value, bool) or isinstance(value, int) and not isinstance(value, bool):
            clean[name] = value
        elif isinstance(value, str):
            clean[name] = value[:80]
    return clean


def _assumptions(settings: BitgetPaperSettings) -> Assumptions:
    return Assumptions(
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
        fee_is_exchange_schedule=False,
        slippage_is_measured_fill=False,
        note=ASSUMPTION_NOTE,
    )


def _source(quote: ValidatedQuote) -> SourceInfo:
    return SourceInfo(
        venue="bitget",
        transport="public_rest_unauthenticated",
        authenticated=False,
        base_url="https://api.bitget.com",
        instruments_path="/api/v3/market/instruments",
        tickers_path="/api/v3/market/tickers",
        query_category="SPOT",
        query_symbol="BTCUSDT",
        instrument_request_time_ms=quote.instrument_request_time_ms,
        ticker_request_time_ms=quote.ticker_request_time_ms,
        ticker_ts_ms=quote.ticker_ts_ms,
    )


def _source_paths(settings: BitgetPaperSettings) -> dict[str, Any]:
    return {
        "venue": "bitget",
        "transport": "public_rest_unauthenticated",
        "authenticated": False,
        "base_url": settings.public_base_url,
        "instruments_path": settings.instruments_path,
        "tickers_path": settings.tickers_path,
        "query_category": "SPOT",
        "query_symbol": "BTCUSDT",
    }


def _apply_mark(state: PaperState, mark_price: Decimal, unrealized: Decimal, now: datetime) -> PaperState:
    return state.model_copy(
        update={
            "last_mark_price": mark_price,
            "last_unrealized_pnl_usd": unrealized,
            "updated_at": now,
        }
    )


def _avg_entry(state: PaperState) -> Decimal:
    if state.quantity_btc == 0:
        return Decimal("0")
    return (state.cost_basis_usd / state.quantity_btc).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
    )


def _summary(
    status: str,
    reason: str,
    fill_written: bool,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "book": "bitget_local_paper",
        "execution": "simulate",
        "authenticated": False,
        "exchange_order": False,
        "symbol": "BTCUSDT",
        "category": "SPOT",
        "demo": True,
        "strategy_expects_profit": False,
        "observation_not_a_signal": True,
        "fill_written": fill_written,
        "label": "PAPER_RULE",
    }
    if extra:
        body.update(extra)
    return body


def _result(status: str, reason: str, exit_code: int, fill_written: bool = False, **extra: Any) -> CycleResult:
    return CycleResult(
        status=status,
        reason=reason,
        fill_written=fill_written,
        exit_code=exit_code,
        summary=_summary(status, reason, fill_written, extra=extra or None),
    )


async def run_cycle(
    config: dict[str, Any],
    *,
    transport=None,
    now: datetime | None = None,
    smoke: bool = False,
    manage_lock: bool = True,
    stop_event: asyncio.Event | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    correlation_id: str | None = None,
    after_commit: Callable[[str], None] | None = None,
    ticker_feed=None,
) -> CycleResult:
    """Run one simulate cycle, or a read-only public smoke when ``smoke`` is set.

    ``config`` may be a full desk file. Only ``bitget_paper`` is interpreted.
    ``mode: live`` on the desk is ignored and cannot redirect this function
    into an exchange executor, because those executors are not imported.
    """
    settings = settings_from_root(config)
    if settings.execution != "simulate":
        raise BitgetPaperConfigError("execution_not_simulate")
    clock = _now(now)
    lock = None
    if manage_lock and not smoke:
        lock = BookLock(lock_path_for(Path(settings.state_path)))
        lock.acquire()
    try:
        return await _run_locked(
            config,
            settings,
            clock,
            transport=transport,
            smoke=smoke,
            stop_event=stop_event,
            sleeper=sleeper or asyncio.sleep,
            correlation_id=correlation_id,
            after_commit=after_commit,
            ticker_feed=ticker_feed,
        )
    finally:
        if lock is not None:
            lock.release()


async def _run_locked(
    config: dict[str, Any],
    settings: BitgetPaperSettings,
    clock: datetime,
    *,
    transport,
    smoke: bool,
    stop_event: asyncio.Event | None,
    sleeper: Callable[[float], Awaitable[None]],
    correlation_id: str | None,
    after_commit: Callable[[str], None] | None,
    ticker_feed=None,
) -> CycleResult:
    # A corrupt book must fail before any network call and before any log write
    # that could be mistaken for a new decision to trade.
    state_path = Path(settings.state_path)
    state = None if smoke else load_state(state_path, settings, clock)
    if state is not None:
        _reconcile_audit(settings, state)
    log = EventLog({"logging": {"path": settings.log_path, "echo_stdout": settings.echo_log}})
    desk_mode = str(config.get("mode", ""))
    correlation = correlation_id or _new_correlation(state)
    client = PublicMarketClient(settings, transport=transport)
    control_path = Path(settings.control_path)
    try:
        early = _control_reason(settings, control_path, stop_event)
        if early == "emergency_stop_holds_position" or early == "shutdown":
            raise _ControlStop(early)
        if ticker_feed is not None and settings.public_ws_enabled and ticker_feed.clock_blocks_trading():
            raise QuoteRejected("clock_anomaly")
        if ticker_feed is not None and settings.public_ws_enabled:
            snap_fn = getattr(ticker_feed, "tradable_snap", None) or ticker_feed.latest
            quote, ws_note = await _fetch_cycle_with_retry(
                client, clock, settings, sleeper, snap_fn, control_path, stop_event,
            )
            ticker_feed.note_trade(quote.source, ws_note)
        else:
            quote = await _fetch_with_retry(client, clock, settings, sleeper, control_path, stop_event)
    except _ControlStop as exc:
        if ticker_feed is not None:
            ticker_feed.note_trade("none", exc.reason)
        return _result(
            "control_held",
            exc.reason,
            3,
            **_carried_book(state, clock, exc.reason),
            correlation_id=correlation,
            desk_mode_ignored=desk_mode,
        )
    except QuoteRejected as exc:
        if ticker_feed is not None:
            ticker_feed.note_trade("none", exc.reason)
        log.write(
            "bitget_paper_quote_rejected",
            book="bitget_local_paper",
            new_fill=False,
            authenticated=False,
            exchange_order=False,
            reason=exc.reason,
            detail=_plain_detail(exc.detail),
            source=_source_paths(settings),
            demo=True,
        )
        if state is not None:
            try:
                rejected = decide_cycle(
                    state,
                    strategy=settings.strategy,
                    quote_accepted=False,
                    notional=settings.demo_notional_usd,
                    candles=None,
                    fast_period=settings.fast_period,
                    slow_period=settings.slow_period,
                    correlation_id=correlation,
                )
            except ContractError:
                return _result("internal_contract", "decision_contract", 1, desk_mode_ignored=desk_mode)
            log.write(
                "bitget_paper_decision",
                book="bitget_local_paper",
                new_fill=False,
                demo=True,
                correlation_id=correlation,
                decision=rejected.model_dump(mode="json"),
            )
        return _result(
            "quote_rejected",
            exc.reason,
            3,
            **_carried_book(state, clock, exc.reason),
            correlation_id=correlation,
            desk_mode_ignored=desk_mode,
        )

    if smoke:
        age_ms = epoch_ms(clock) - quote.ticker_ts_ms
        log.write(
            "bitget_paper_smoke",
            book="bitget_local_paper",
            new_fill=False,
            authenticated=False,
            exchange_order=False,
            observation_not_a_signal=True,
            symbol="BTCUSDT",
            category="SPOT",
            last_price=money_str(quote.last),
            ticker_age_ms=age_ms,
            source=_source(quote).model_dump(mode="json"),
            demo=True,
        )
        return _result(
            "smoke_ok",
            "public_quote_accepted",
            0,
            last_price=money_str(quote.last),
            ticker_age_ms=age_ms,
            desk_mode_ignored=desk_mode,
        )

    if state is None:
        return _result("internal_contract", "missing_state", 1, desk_mode_ignored=desk_mode)
    candles = None
    if settings.strategy != "deterministic_demo_flat_once_v1":
        try:
            candles = await _fetch_candles_with_retry(
                client, clock, settings, sleeper, control_path, stop_event,
            )
        except _ControlStop as exc:
            if ticker_feed is not None:
                ticker_feed.note_trade("none", exc.reason)
            return _result(
                "control_held",
                exc.reason,
                3,
                **_carried_book(state, clock, exc.reason),
                correlation_id=correlation,
                desk_mode_ignored=desk_mode,
            )
        except QuoteRejected as exc:
            log.write(
                "bitget_paper_quote_rejected",
                book="bitget_local_paper",
                new_fill=False,
                authenticated=False,
                exchange_order=False,
                reason=exc.reason,
                detail=_plain_detail(exc.detail),
                correlation_id=correlation,
                demo=True,
            )
            return _result(
                "quote_rejected",
                exc.reason,
                3,
                **_carried_book(state, clock, exc.reason),
                correlation_id=correlation,
                desk_mode_ignored=desk_mode,
            )
    try:
        control = read_control(Path(settings.control_path))
        decision = decide_cycle(
            state,
            strategy=settings.strategy,
            quote_accepted=True,
            notional=settings.demo_notional_usd,
            candles=candles,
            fast_period=settings.fast_period,
            slow_period=settings.slow_period,
            correlation_id=correlation,
        )
    except (ContractError, StateError) as exc:
        reason = getattr(exc, "reason", "decision_contract")
        if isinstance(exc, StateError):
            raise
        return _result("internal_contract", reason, 1, correlation_id=correlation, desk_mode_ignored=desk_mode)

    state = state.model_copy(update={"cycle_seq": state.cycle_seq + 1})
    log.write(
        "bitget_paper_decision",
        book="bitget_local_paper",
        new_fill=False,
        demo=True,
        correlation_id=correlation,
        decision=decision.model_dump(mode="json"),
        source=_source(quote).model_dump(mode="json"),
        assumptions=_assumptions(settings).model_dump(mode="json"),
    )

    try:
        mark_price, unrealized = mark_position(
            state, quote, settings.fee_bps, settings.slippage_bps
        )
    except BrokerRefusal as exc:
        log.write(
            "bitget_paper_risk_rejected",
            book="bitget_local_paper",
            new_fill=False,
            authenticated=False,
            exchange_order=False,
            reason=exc.reason,
            demo=True,
        )
        return _result("risk_rejected", exc.reason, 0, desk_mode_ignored=desk_mode)

    action = decision.action
    if (
        control.mode == "emergency_stop"
        and settings.emergency_flatten
        and state.quantity_btc > 0
        and not state.emergency_flattened
    ):
        action = "SELL"
        payload = decision.model_dump()
        payload["action"] = "SELL"
        payload["reason"] = "emergency_flatten"
        payload["notional_usd"] = money_str(state.quantity_btc * quote.bid)
        decision = DemoDecision.model_validate(payload).checked()
    blocked = _control_block(control.mode, action, settings.emergency_flatten)
    if blocked:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, blocked, correlation, desk_mode,
        )
    if stop_event is not None and stop_event.is_set() and action in {"BUY", "SELL"}:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, "shutdown", correlation, desk_mode,
        )
    # One simulated fill per closed candle. The side does not matter: the same
    # bar cannot buy and then sell. Emergency flatten is the operator exception.
    if (
        action in {"BUY", "SELL"}
        and decision.basis.bar_ts_ms
        and state.last_bar_ms == decision.basis.bar_ts_ms
        and decision.reason != "emergency_flatten"
    ):
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, "duplicate_bar", correlation, desk_mode,
        )
    if correlation in state.applied_ids and action in {"BUY", "SELL"}:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, "duplicate_decision", correlation, desk_mode,
        )

    if action != "BUY":
        if action != "SELL":
            marked = _stamp(state, quote, settings, mark_price, unrealized, clock, action, False, None, correlation)
            save_state(state_path, marked)
            _write_mark(log, marked, quote, settings, mark_price, unrealized, correlation)
            _write_equity(settings, marked, unrealized, correlation)
            return _book_result(
                "held" if action == "HOLD" else "no_trade",
                decision.reason,
                False,
                marked,
                unrealized,
                correlation,
                desk_mode,
                action,
                epoch_ms(clock) - quote.ticker_ts_ms,
                clock.isoformat(),
                quote,
                mark_price,
                decision,
            )
        return _sell(
            state, quote, settings, log, state_path, clock, decision, correlation, desk_mode,
            control.mode, after_commit,
        )

    gate = PaperRiskGate(settings.max_position_usd, settings.max_loss_usd)
    allowed, risk_reason = gate.approve_buy(
        exposure_usd=exposure_usd(state, quote),
        realized_pnl_usd=state.realized_pnl_usd,
        unrealized_pnl_usd=unrealized,
        buy_notional_usd=settings.demo_notional_usd,
        fee_bps=settings.fee_bps,
    )
    if not allowed:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, risk_reason, correlation, desk_mode,
        )
    equity = equity_of(state, unrealized)
    if drawdown_ratio(max(state.peak_equity_usd, equity), equity) >= settings.max_drawdown_pct:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, "max_drawdown", correlation, desk_mode,
        )

    try:
        bought = simulate_buy(
            state, quote, settings.demo_notional_usd, settings.fee_bps, settings.slippage_bps, clock,
        )
        mark_price, unrealized = mark_position(bought, quote, settings.fee_bps, settings.slippage_bps)
        bought = _stamp(
            bought, quote, settings, mark_price, unrealized, clock, "BUY", True,
            decision.basis.bar_ts_ms, correlation,
        )
        record = _fill_record(bought, state, quote, settings, decision, mark_price, unrealized, "BUY")
    except (BrokerRefusal, ContractError) as exc:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, exc.reason, correlation, desk_mode,
        )

    save_state(state_path, bought)
    # The book is committed before the audit line. A crash here keeps the
    # position and drops the fill row; the next cycle records the gap.
    if after_commit is not None:
        after_commit("buy")
    log.write("bitget_paper_fill", **record.model_dump(mode="json"), correlation_id=correlation)
    _write_mark(log, bought, quote, settings, mark_price, unrealized, correlation)
    _write_equity(settings, bought, unrealized, correlation)
    return _book_result(
        "filled", decision.reason, True, bought, unrealized, correlation, desk_mode, "BUY",
        epoch_ms(clock) - quote.ticker_ts_ms, clock.isoformat(), quote, mark_price, decision,
    )


def _carried_book(state: PaperState | None, clock: datetime, reason: str) -> dict[str, Any]:
    """Last committed book. The failed quote is not a new mark."""
    body: dict[str, Any] = {
        "book_stale": True,
        "observed_at": clock.isoformat(),
        "book_note": reason,
    }
    if state is None:
        return body
    unrealized = state.last_unrealized_pnl_usd
    body.update(
        {
            "cash_usd": money_str(state.cash_usd),
            "quantity_btc": money_str(state.quantity_btc),
            "realized_usd": money_str(state.realized_pnl_usd, signed=True),
            "unrealized_usd": money_str(unrealized, signed=True),
            "equity_usd": money_str(equity_of(state, unrealized), signed=True),
            "book_as_of": state.updated_at.isoformat(),
            "equity_tail": [point.model_dump() for point in state.equity_tail],
        }
    )
    return body


def _reconcile_audit(settings: BitgetPaperSettings, state: PaperState) -> None:
    """If the book committed a fill the log never received, say so once.

    This does not rebuild the missing fill. The state file stays authoritative.
    """
    if not state.applied_ids:
        return
    latest = state.applied_ids[-1]
    path = Path(settings.log_path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("correlation_id") != latest:
                continue
            if row.get("type") in {"bitget_paper_fill", "bitget_paper_audit_gap"}:
                return
    append_jsonl(
        path,
        {
            "type": "bitget_paper_audit_gap",
            "book": "bitget_local_paper",
            "correlation_id": latest,
            "book_authoritative": True,
            "fill_count": state.fill_count,
            "quantity_btc": money_str(state.quantity_btc),
            "note": (
                "state file was committed before the fill audit line; "
                "this row does not reconstruct that fill"
            ),
        },
    )


def _new_correlation(state: PaperState | None) -> str:
    seq = 0 if state is None else state.cycle_seq + 1
    return f"c{seq:06d}{uuid.uuid4().hex[:8]}"


def _retryable(exc: QuoteRejected) -> bool:
    if exc.reason == "transport_error":
        return True
    if exc.reason == "http_status":
        status = exc.detail.get("status")
        return status == 429 or (isinstance(status, int) and not isinstance(status, bool) and status >= 500)
    return False


class _ControlStop(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _control_reason(settings: BitgetPaperSettings, control_path: Path, stop_event: asyncio.Event | None) -> str | None:
    if stop_event is not None and stop_event.is_set():
        return "shutdown"
    mode = read_control(control_path).mode
    if mode == "emergency_stop" and not settings.emergency_flatten:
        return "emergency_stop_holds_position"
    if mode == "paused":
        return "paused"
    return None


async def _await_unless_control(awaitable, settings, control_path, stop_event):
    """Give pause and emergency stop a chance while a request is still running.

    A healthy request that finishes inside the poll window is unchanged.
    A hung request is cancelled on the next poll instead of waiting out every retry.
    """
    task = asyncio.create_task(awaitable)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=0.2)
            if task in done:
                return task.result()
            reason = _control_reason(settings, control_path, stop_event)
            if reason:
                raise _ControlStop(reason)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _backoff_unless_control(settings, control_path, stop_event, sleeper, attempt: int) -> None:
    reason = _control_reason(settings, control_path, stop_event)
    if reason:
        raise _ControlStop(reason)
    await sleeper(float(settings.retry_backoff_seconds) * (2 ** attempt))
    reason = _control_reason(settings, control_path, stop_event)
    if reason:
        raise _ControlStop(reason)


async def _fetch_cycle_with_retry(
    client: PublicMarketClient,
    clock: datetime,
    settings: BitgetPaperSettings,
    sleeper,
    snap_fn,
    control_path: Path,
    stop_event: asyncio.Event | None,
):
    attempts = settings.max_cycle_retries + 1
    last: QuoteRejected | None = None
    for attempt in range(attempts):
        try:
            return await _await_unless_control(client.fetch_cycle(clock, snap_fn()), settings, control_path, stop_event)
        except _ControlStop:
            raise
        except QuoteRejected as exc:
            last = exc
            if not _retryable(exc) or attempt + 1 >= attempts:
                raise
            await _backoff_unless_control(settings, control_path, stop_event, sleeper, attempt)
    assert last is not None
    raise last


async def _fetch_with_retry(
    client: PublicMarketClient,
    clock: datetime,
    settings: BitgetPaperSettings,
    sleeper,
    control_path: Path,
    stop_event: asyncio.Event | None,
) -> ValidatedQuote:
    attempts = settings.max_cycle_retries + 1
    last: QuoteRejected | None = None
    for attempt in range(attempts):
        try:
            return await _await_unless_control(client.fetch(clock), settings, control_path, stop_event)
        except _ControlStop:
            raise
        except QuoteRejected as exc:
            last = exc
            if not _retryable(exc) or attempt + 1 >= attempts:
                raise
            await _backoff_unless_control(settings, control_path, stop_event, sleeper, attempt)
    assert last is not None
    raise last


async def _fetch_candles_with_retry(
    client: PublicMarketClient,
    clock: datetime,
    settings: BitgetPaperSettings,
    sleeper,
    control_path: Path,
    stop_event: asyncio.Event | None,
):
    attempts = settings.max_cycle_retries + 1
    last: QuoteRejected | None = None
    for attempt in range(attempts):
        try:
            return await _await_unless_control(client.fetch_candles(clock), settings, control_path, stop_event)
        except _ControlStop:
            raise
        except QuoteRejected as exc:
            last = exc
            if not _retryable(exc) or attempt + 1 >= attempts:
                raise
            await _backoff_unless_control(settings, control_path, stop_event, sleeper, attempt)
    assert last is not None
    raise last


def _control_block(mode: str, action: str, emergency_flatten: bool) -> str | None:
    if mode == "paused" and action == "BUY":
        return "paused"
    if mode == "emergency_stop" and action in {"BUY", "SELL"} and not emergency_flatten:
        return "emergency_stop_holds_position"
    if mode == "emergency_stop" and action == "BUY":
        return "emergency_stop_holds_position"
    return None


def _stamp(
    state: PaperState,
    quote: ValidatedQuote,
    settings: BitgetPaperSettings,
    mark_price: Decimal,
    unrealized: Decimal,
    clock: datetime,
    action: str,
    fill: bool,
    bar_ms: int | None,
    correlation: str,
) -> PaperState:
    marked = _apply_mark(state, mark_price, unrealized, clock)
    equity = equity_of(marked, unrealized)
    stamped = remember_mark(
        marked,
        equity=equity,
        unrealized=unrealized,
        now_iso=clock.isoformat(),
        action=action,
        bar_ms=bar_ms,
        correlation_id=correlation,
        fill=fill,
    )
    if settings.emergency_flatten and action == "SELL" and fill and stamped.quantity_btc == 0:
        stamped = stamped.model_copy(update={"emergency_flattened": True})
    return stamped


def _book_result(
    status: str,
    reason: str,
    fill_written: bool,
    state: PaperState,
    unrealized: Decimal,
    correlation: str,
    desk_mode: str,
    action: str,
    ticker_age_ms: int | None = None,
    observed_at: str = "",
    quote: ValidatedQuote | None = None,
    mark_price: Decimal | None = None,
    decision: DemoDecision | None = None,
) -> CycleResult:
    """Book summary plus display fields. The extra strings do not change the fill."""
    equity = equity_of(state, unrealized)
    extra: dict[str, Any] = {}
    if quote is not None:
        extra["last_price"] = money_str(quote.last)
    if mark_price is not None:
        extra["mark_price"] = money_str(mark_price)
    if decision is not None:
        extra["fast_sma"] = decision.basis.fast_sma
        extra["slow_sma"] = decision.basis.slow_sma
    return _result(
        status,
        reason,
        0,
        fill_written,
        correlation_id=correlation,
        action=action,
        ts=observed_at,
        ticker_age_ms=ticker_age_ms if ticker_age_ms is not None else "",
        quantity_btc=money_str(state.quantity_btc),
        cash_usd=money_str(state.cash_usd),
        unrealized_usd=money_str(unrealized, signed=True),
        realized_usd=money_str(state.realized_pnl_usd, signed=True),
        equity_usd=money_str(equity, signed=True),
        equity_tail=[point.model_dump() for point in state.equity_tail],
        desk_mode_ignored=desk_mode,
        quote_source=quote.source if quote is not None else "",
        **extra,
    )


def _reject(
    state, quote, settings, log, state_path, clock, mark_price, unrealized,
    decision, reason, correlation, desk_mode,
) -> CycleResult:
    marked = _stamp(state, quote, settings, mark_price, unrealized, clock, decision.action, False, None, correlation)
    save_state(state_path, marked)
    log.write(
        "bitget_paper_risk_rejected",
        book="bitget_local_paper",
        new_fill=False,
        authenticated=False,
        exchange_order=False,
        reason=reason,
        correlation_id=correlation,
        decision=decision.model_dump(mode="json"),
        demo=True,
    )
    _write_mark(log, marked, quote, settings, mark_price, unrealized, correlation)
    _write_equity(settings, marked, unrealized, correlation)
    return _book_result(
        "risk_rejected", reason, False, marked, unrealized, correlation, desk_mode, decision.action,
        epoch_ms(clock) - quote.ticker_ts_ms, clock.isoformat(), quote, mark_price, decision,
    )


def _sell(state, quote, settings, log, state_path, clock, decision, correlation, desk_mode, control_mode, after_commit) -> CycleResult:
    if state.quantity_btc <= 0:
        mark_price, unrealized = mark_position(state, quote, settings.fee_bps, settings.slippage_bps)
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, "no_position", correlation, desk_mode,
        )
    mark_price, unrealized = mark_position(state, quote, settings.fee_bps, settings.slippage_bps)
    try:
        sold = simulate_sell_all(state, quote, settings.fee_bps, settings.slippage_bps, clock)
        mark_price, unrealized = mark_position(sold, quote, settings.fee_bps, settings.slippage_bps)
        sold = _stamp(
            sold, quote, settings, mark_price, unrealized, clock, "SELL", True,
            decision.basis.bar_ts_ms, correlation,
        )
        if control_mode == "emergency_stop":
            sold = sold.model_copy(update={"emergency_flattened": True})
        record = _fill_record(sold, state, quote, settings, decision, mark_price, unrealized, "SELL")
    except (BrokerRefusal, ContractError) as exc:
        return _reject(
            state, quote, settings, log, state_path, clock, mark_price, unrealized,
            decision, exc.reason, correlation, desk_mode,
        )
    save_state(state_path, sold)
    if after_commit is not None:
        after_commit("sell")
    log.write("bitget_paper_fill", **record.model_dump(mode="json"), correlation_id=correlation)
    _write_mark(log, sold, quote, settings, mark_price, unrealized, correlation)
    _write_equity(settings, sold, unrealized, correlation)
    return _book_result(
        "filled", decision.reason, True, sold, unrealized, correlation, desk_mode, "SELL",
        epoch_ms(clock) - quote.ticker_ts_ms, clock.isoformat(), quote, mark_price, decision,
    )


def _write_equity(settings: BitgetPaperSettings, state: PaperState, unrealized: Decimal, correlation: str) -> None:
    if not state.equity_tail:
        return
    point = state.equity_tail[-1].model_dump()
    append_jsonl(
        Path(settings.equity_path),
        {"type": "bitget_paper_equity", "correlation_id": correlation, "book": "bitget_local_paper", **point},
    )


def _write_mark(
    log: EventLog,
    state: PaperState,
    quote: ValidatedQuote,
    settings: BitgetPaperSettings,
    mark_price: Decimal,
    unrealized: Decimal,
    correlation: str,
) -> None:
    total = state.realized_pnl_usd + unrealized
    log.write(
        "bitget_paper_mark",
        book="bitget_local_paper",
        new_fill=False,
        demo=True,
        symbol="BTCUSDT",
        category="SPOT",
        source=_source(quote).model_dump(mode="json"),
        assumptions=_assumptions(settings).model_dump(mode="json"),
        position={
            "quantity_btc": money_str(state.quantity_btc),
            "cost_basis_usd": money_str(state.cost_basis_usd),
            "cash_usd": money_str(state.cash_usd),
            "fill_count": state.fill_count,
        },
        pnl={
            "realized_usd": money_str(state.realized_pnl_usd, signed=True),
            "unrealized_usd": money_str(unrealized, signed=True),
            "total_usd": money_str(total, signed=True),
            "mark_price": money_str(mark_price),
        },
        correlation_id=correlation,
    )


def _fill_record(
    bought: PaperState,
    previous: PaperState,
    quote: ValidatedQuote,
    settings: BitgetPaperSettings,
    decision: Any,
    mark_price: Decimal,
    unrealized: Decimal,
    side: str,
) -> PaperFillRecord:
    fee = bought.fees_paid_usd - previous.fees_paid_usd
    if side == "BUY":
        debit = previous.cash_usd - bought.cash_usd
        credit = Decimal("0")
        gross = debit - fee
        quantity = bought.quantity_btc - previous.quantity_btc
    else:
        debit = Decimal("0")
        credit = bought.cash_usd - previous.cash_usd
        gross = credit + fee
        quantity = previous.quantity_btc - bought.quantity_btc
    price = (gross / quantity).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
    total = bought.realized_pnl_usd + unrealized
    return PaperFillRecord(
        book="bitget_local_paper",
        label="LOCAL_SIMULATED_FILL",
        new_fill=True,
        not_exchange_order=True,
        authenticated=False,
        strategy_expects_profit=False,
        demo=True,
        symbol="BTCUSDT",
        category="SPOT",
        side=side,
        decision=decision,
        source=_source(quote),
        assumptions=_assumptions(settings),
        quote=QuoteSnapshot(
            bid=money_str(quote.bid),
            ask=money_str(quote.ask),
            last=money_str(quote.last),
            bid_size=money_str(quote.bid_size),
            ask_size=money_str(quote.ask_size),
        ),
        fill=FillEconomics(
            price=money_str(price),
            quantity_btc=money_str(quantity),
            gross_usd=money_str(gross),
            fee_usd=money_str(fee),
            cash_debited_usd=money_str(debit),
            cash_credited_usd=money_str(credit),
        ),
        position=PositionSnapshot(
            quantity_btc=money_str(bought.quantity_btc),
            cost_basis_usd=money_str(bought.cost_basis_usd),
            cash_usd=money_str(bought.cash_usd),
            avg_entry_price=money_str(_avg_entry(bought)),
            fees_paid_usd=money_str(bought.fees_paid_usd),
            fill_count=bought.fill_count,
        ),
        pnl=PnlSnapshot(
            realized_usd=money_str(bought.realized_pnl_usd, signed=True),
            unrealized_usd=money_str(unrealized, signed=True),
            total_usd=money_str(total, signed=True),
            mark_price=money_str(mark_price),
            mark_side="bid_after_adverse_slippage_and_fee",
        ),
    )
