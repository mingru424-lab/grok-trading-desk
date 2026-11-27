"""Continuous paper loop. One cycle at a time, then a bounded sleep.

Stop between cycles, or before a fill if the stop flag is already set.
The status server, when enabled, is the same process and reads the snapshot
this loop publishes.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import settings_from_root
from .engine import run_cycle
from .market import PublicMarketClient
from .models import QuoteRejected, epoch_ms
from .public_ws import PublicTickerFeed, require_websockets
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
    book_lock = BookLock(lock_path_for(Path(settings.state_path)))
    book_lock.acquire()
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
    feed: PublicTickerFeed | None = None
    feed_task: asyncio.Task | None = None
    clock_task: asyncio.Task | None = None
    lock = threading.Lock()
    own_stop = stop_event or asyncio.Event()
    try:
        if settings.public_ws_enabled and transport is None:
            require_websockets()
            feed = PublicTickerFeed(settings, now_fn=clock)
            published["market"] = feed.view()
            feed_task = asyncio.create_task(feed.run(own_stop))
            clock_task = asyncio.create_task(_clock_sync(feed, settings, own_stop, pause))
        else:
            published["market"] = {
                "enabled": settings.public_ws_enabled,
                "connection": "disabled" if not settings.public_ws_enabled else "not_started",
                "subscription": "none",
                "heartbeat": "idle",
                "trade_source": "bitget_public_rest_v3",
                "trade_note": "ws_disabled" if not settings.public_ws_enabled else "injected_transport",
            }
        if settings.status_enabled:
            server = StatusServer(
                settings.status_host,
                settings.status_port,
                lambda: _copy_status(published, lock, feed),
            )
            server.start()
            published["status_port"] = server.bound_port
        published["runtime"] = "running"
        if feed is not None:
            waited = 0
            while feed.latest() is None and waited < 30 and not own_stop.is_set():
                await pause(0.1)
                waited += 1
        completed = 0
        while max_cycles is None or completed < max_cycles:
            if own_stop.is_set():
                break
            if feed is not None:
                with lock:
                    published["market"] = feed.view()
            result = await run_cycle(
                config,
                transport=transport,
                now=clock(),
                manage_lock=False,
                stop_event=own_stop,
                sleeper=pause,
                ticker_feed=feed,
            )
            results.append(result.summary)
            with lock:
                published["runtime"] = read_control(Path(settings.control_path)).mode
                published["feed"] = _feed(published.get("feed") or {}, result)
                if feed is not None:
                    published["market"] = feed.view()
                published["strategy"] = _strategy(published.get("strategy") or {}, result.summary)
                published["last_decision"] = {
                    "action": result.summary.get("action", ""),
                    "reason": result.reason,
                    "correlation_id": result.summary.get("correlation_id", ""),
                    "ts": result.summary.get("ts", ""),
                    "fill_written": bool(result.summary.get("fill_written")),
                }
                recent = list(published["recent_decisions"])
                recent.append(published["last_decision"])
                published["recent_decisions"] = recent[-20:]
                _keep_book(published, result.summary)
                if result.summary.get("equity_tail"):
                    published["equity"] = result.summary["equity_tail"]
                write_json(Path(settings.status_path), published)
            completed += 1
            if own_stop.is_set():
                break
            if max_cycles is not None and completed >= max_cycles:
                break
            stopped = await _wait(pause, settings.poll_seconds, own_stop)
            if stopped:
                break
        published["runtime"] = "stopped"
        write_json(Path(settings.status_path), published)
        return {"cycles": len(results), "results": results, "status_port": published.get("status_port")}
    finally:
        if feed is not None:
            await feed.stop()
        if feed_task is not None:
            feed_task.cancel()
            try:
                await feed_task
            except (asyncio.CancelledError, Exception):
                pass
        if clock_task is not None:
            clock_task.cancel()
            try:
                await clock_task
            except (asyncio.CancelledError, Exception):
                pass
        if server is not None:
            server.stop()
        book_lock.release()


def _feed(previous: dict[str, Any], result: Any) -> dict[str, Any]:
    """Quote display. A rejected cycle keeps the last price and marks it stale."""
    summary = result.summary
    status = "ok" if result.exit_code == 0 else "rejected"
    body = {
        "status": status,
        "reason": result.reason,
        "ticker_age_ms": summary.get("ticker_age_ms", ""),
        "as_of": summary.get("observed_at") or summary.get("ts") or "",
        "quote_source": summary.get("quote_source") or "",
    }
    price = summary.get("last_price") or ""
    mark = summary.get("mark_price") or ""
    if price:
        body["last_price"] = price
        body["price_stale"] = status != "ok"
    else:
        body["last_price"] = previous.get("last_price", "")
        body["price_stale"] = bool(body["last_price"])
    if mark:
        body["mark_price"] = mark
    else:
        body["mark_price"] = previous.get("mark_price", "")
    return body


def _strategy(previous: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    body = dict(previous)
    fast = summary.get("fast_sma") or ""
    slow = summary.get("slow_sma") or ""
    if fast:
        body["fast_sma"] = fast
        body["slow_sma"] = slow
    return body


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


def _copy_status(published: dict[str, Any], lock: threading.Lock, feed: PublicTickerFeed | None) -> dict[str, Any]:
    """Copy under the lock. Refresh the stream view so the page is not stuck on the last decision."""
    with lock:
        if feed is not None:
            published["market"] = feed.view()
        body = copy.deepcopy(published)
    body["served_at_ms"] = epoch_ms(datetime.now(timezone.utc))
    return body


async def _clock_sync(feed: PublicTickerFeed, settings, stop: asyncio.Event, pause: SleepFn) -> None:
    client = PublicMarketClient(settings)
    while not stop.is_set() and not feed._stop.is_set():
        try:
            sample = await client.measure_clock()
            feed.set_clock(
                sample.status,
                sample.offset_ms,
                sample.rtt_ms,
                sample.uncertainty_ms,
                sample.server_time_ms,
            )
        except QuoteRejected as exc:
            feed.set_clock_failed(exc.reason)
        except Exception as exc:  # noqa: BLE001 - clock sync must not kill the book
            feed.set_clock_failed(type(exc).__name__)
        stopped = await _wait(pause, settings.clock_sync_seconds, stop)
        if stopped:
            break


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
