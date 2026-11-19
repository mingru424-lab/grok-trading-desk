"""One public-quote cycle: validate, decide, maybe simulate, always record.

The state file is the book of record. It is written before the fill line in
the log, so a crash between the two cannot produce a second simulated fill
on the next run. A quote that fails validation does not touch the state file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from ..shared.log import EventLog
from .broker import exposure_usd, mark_position, simulate_buy
from .config import BitgetPaperConfigError, BitgetPaperSettings, settings_from_root
from .decision import demo_decide
from .market import PublicMarketClient
from .models import (
    ASSUMPTION_NOTE,
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
        version=1,
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
        updated_at=now,
    )


def load_state(path: Path, settings: BitgetPaperSettings, now: datetime) -> PaperState:
    if not path.exists():
        return fresh_state(settings, now)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = PaperState.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        raise StateError("state_invalid") from None
    if state.symbol != settings.symbol or state.category != settings.category:
        raise StateError("state_symbol_mismatch")
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
        "label": "DETERMINISTIC_DEMO",
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
    # A corrupt book must fail before any network call and before any log write
    # that could be mistaken for a new decision to trade.
    state_path = Path(settings.state_path)
    state = None if smoke else load_state(state_path, settings, clock)
    log = EventLog({"logging": {"path": settings.log_path, "echo_stdout": settings.echo_log}})
    desk_mode = str(config.get("mode", ""))

    client = PublicMarketClient(settings, transport=transport)
    try:
        quote = await client.fetch(clock)
    except QuoteRejected as exc:
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
                rejected = demo_decide(state, False, settings.demo_notional_usd)
            except ContractError:
                return _result("internal_contract", "decision_contract", 1, desk_mode_ignored=desk_mode)
            log.write(
                "bitget_paper_decision",
                book="bitget_local_paper",
                new_fill=False,
                demo=True,
                decision=rejected.model_dump(mode="json"),
            )
        return _result(
            "quote_rejected",
            exc.reason,
            3,
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
    try:
        decision = demo_decide(state, True, settings.demo_notional_usd)
    except ContractError:
        return _result("internal_contract", "decision_contract", 1, desk_mode_ignored=desk_mode)

    log.write(
        "bitget_paper_decision",
        book="bitget_local_paper",
        new_fill=False,
        demo=True,
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

    if decision.action != "BUY":
        marked = _apply_mark(state, mark_price, unrealized, clock)
        save_state(state_path, marked)
        _write_mark(log, marked, quote, settings, mark_price, unrealized)
        return _result(
            "held" if decision.action == "HOLD" else "no_trade",
            decision.reason,
            0,
            quantity_btc=money_str(marked.quantity_btc),
            unrealized_usd=money_str(unrealized, signed=True),
            realized_usd=money_str(marked.realized_pnl_usd, signed=True),
            desk_mode_ignored=desk_mode,
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
        marked = _apply_mark(state, mark_price, unrealized, clock)
        save_state(state_path, marked)
        log.write(
            "bitget_paper_risk_rejected",
            book="bitget_local_paper",
            new_fill=False,
            authenticated=False,
            exchange_order=False,
            reason=risk_reason,
            notional_usd=money_str(settings.demo_notional_usd),
            max_position_usd=money_str(settings.max_position_usd),
            max_loss_usd=money_str(settings.max_loss_usd),
            decision=decision.model_dump(mode="json"),
            demo=True,
        )
        _write_mark(log, marked, quote, settings, mark_price, unrealized)
        return _result(
            "risk_rejected",
            risk_reason,
            0,
            quantity_btc=money_str(marked.quantity_btc),
            desk_mode_ignored=desk_mode,
        )

    try:
        bought = simulate_buy(
            state,
            quote,
            settings.demo_notional_usd,
            settings.fee_bps,
            settings.slippage_bps,
            clock,
        )
        mark_price, unrealized = mark_position(
            bought, quote, settings.fee_bps, settings.slippage_bps
        )
        bought = _apply_mark(bought, mark_price, unrealized, clock)
        record = _fill_record(bought, state, quote, settings, decision, mark_price, unrealized)
    except (BrokerRefusal, ContractError) as exc:
        marked = _apply_mark(state, mark_price, unrealized, clock)
        save_state(state_path, marked)
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

    save_state(state_path, bought)
    log.write("bitget_paper_fill", **record.model_dump(mode="json"))
    _write_mark(log, bought, quote, settings, mark_price, unrealized)
    return _result(
        "filled",
        decision.reason,
        0,
        True,
        quantity_btc=money_str(bought.quantity_btc),
        unrealized_usd=money_str(unrealized, signed=True),
        realized_usd=money_str(bought.realized_pnl_usd, signed=True),
        desk_mode_ignored=desk_mode,
    )


def _write_mark(
    log: EventLog,
    state: PaperState,
    quote: ValidatedQuote,
    settings: BitgetPaperSettings,
    mark_price: Decimal,
    unrealized: Decimal,
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
    )


def _fill_record(
    bought: PaperState,
    previous: PaperState,
    quote: ValidatedQuote,
    settings: BitgetPaperSettings,
    decision: Any,
    mark_price: Decimal,
    unrealized: Decimal,
) -> PaperFillRecord:
    debit = previous.cash_usd - bought.cash_usd
    fee = bought.fees_paid_usd - previous.fees_paid_usd
    gross = debit - fee
    quantity = bought.quantity_btc - previous.quantity_btc
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
        side="BUY",
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
