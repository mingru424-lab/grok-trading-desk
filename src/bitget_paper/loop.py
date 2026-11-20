"""Continuous paper loop. One cycle at a time, then a bounded sleep.

Stop between cycles, or before a fill if the stop flag is already set.
The status server, when enabled, is the same process and reads the snapshot
this loop publishes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import settings_from_root
from .engine import run_cycle
from .status_server import StatusServer
from .store import BookLock, lock_path_for, read_control, write_json
from pathlib import Path


SleepFn = Callable[[float], Awaitable[None]]


def _snapshot(base: dict[str, Any]) -> dict[str, Any]:
    body = {
        "banner": "PAPER ONLY",
        "banner_zh": "本地纸面，不是交易所订单，不代表收益",
        "execution": "simulate",
        "authenticated": False,
        "exchange_order": False,
        "strategy_expects_profit": False,
        "not_llm": True,
        "symbol": "BTCUSDT",
        "category": "SPOT",
    }
    body.update(base)
    return body


async def run_loop(
    config: dict[str, Any],
    *,
    transport=None,
    max_cycles: int | None = None,
    sleep: SleepFn | None = None,
    stop_event: asyncio.Event | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    settings = settings_from_root(config)
    pause = sleep or asyncio.sleep
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    lock = BookLock(lock_path_for(Path(settings.state_path)))
    lock.acquire()
    published: dict[str, Any] = _snapshot(
        {
            "runtime": "starting",
            "feed": {"status": "unknown"},
            "strategy": {
                "id": settings.strategy,
                "fast_period": settings.fast_period,
                "slow_period": settings.slow_period,
                "interval": settings.candle_interval,
            },
            "risk": {
                "max_position_usd": str(settings.max_position_usd),
                "max_loss_usd": str(settings.max_loss_usd),
                "max_drawdown_pct": str(settings.max_drawdown_pct),
                "emergency_flatten": settings.emergency_flatten,
                "fee_bps": settings.fee_bps,
                "slippage_bps": settings.slippage_bps,
            },
            "book": {},
            "last_decision": {},
            "recent_decisions": [],
            "equity": [],
        }
    )
    server = None
    results: list[dict[str, Any]] = []
    try:
        if settings.status_enabled:
            server = StatusServer(settings.status_host, settings.status_port, lambda: dict(published))
            server.start()
            published["status_port"] = server.bound_port
        published["runtime"] = "running"
        completed = 0
        while max_cycles is None or completed < max_cycles:
            if stop_event is not None and stop_event.is_set():
                break
            result = await run_cycle(
                config,
                transport=transport,
                now=clock(),
                manage_lock=False,
                stop_event=stop_event,
                sleeper=pause,
            )
            results.append(result.summary)
            published["runtime"] = read_control(Path(settings.control_path)).mode
            published["feed"] = {
                "status": "ok" if result.exit_code == 0 else "rejected",
                "reason": result.reason,
                "ticker_age_ms": result.summary.get("ticker_age_ms", ""),
                "as_of": result.summary.get("observed_at") or result.summary.get("ts") or "",
            }
            published["last_decision"] = {
                "action": result.summary.get("action", ""),
                "reason": result.reason,
                "correlation_id": result.summary.get("correlation_id", ""),
                "ts": result.summary.get("ts", ""),
            }
            recent = list(published["recent_decisions"])
            recent.append(published["last_decision"])
            published["recent_decisions"] = recent[-20:]
            _keep_book(published, result.summary)
            if result.summary.get("equity_tail"):
                published["equity"] = result.summary["equity_tail"]
            write_json(Path(settings.status_path), published)
            completed += 1
            if stop_event is not None and stop_event.is_set():
                break
            if max_cycles is not None and completed >= max_cycles:
                break
            stopped = await _wait(pause, settings.poll_seconds, stop_event)
            if stopped:
                break
        published["runtime"] = "stopped"
        write_json(Path(settings.status_path), published)
        return {"cycles": len(results), "results": results, "status_port": published.get("status_port")}
    finally:
        if server is not None:
            server.stop()
        lock.release()


def _keep_book(published: dict[str, Any], summary: dict[str, Any]) -> None:
    """A rejected quote must not replace a known book with blanks."""
    cash = summary.get("cash_usd", "")
    quantity = summary.get("quantity_btc", "")
    has_numbers = cash != "" and quantity != ""
    stale = bool(summary.get("book_stale")) or summary.get("status") == "quote_rejected"
    if has_numbers:
        published["book"] = {
            "cash_usd": cash,
            "quantity_btc": quantity,
            "realized_usd": summary.get("realized_usd", ""),
            "unrealized_usd": summary.get("unrealized_usd", ""),
            "equity_usd": summary.get("equity_usd", ""),
            "stale": stale,
            "as_of": summary.get("book_as_of") or summary.get("observed_at") or summary.get("ts") or "",
        }
        return
    if published.get("book"):
        published["book"]["stale"] = True
        published["book"]["as_of"] = summary.get("observed_at") or published["book"].get("as_of", "")


async def _wait(pause: SleepFn, seconds: float, stop_event: asyncio.Event | None) -> bool:
    if stop_event is None:
        await pause(seconds)
        return False
    sleep_task = asyncio.create_task(pause(seconds))
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return stop_task in done
