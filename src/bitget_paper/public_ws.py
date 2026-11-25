"""Public BTCUSDT spot ticker stream.

Protocol, read from Bitget's current UTA documents, not from a private channel:

* URL ``wss://ws.bitget.com/v3/ws/public``
* subscribe ``{"op":"subscribe","args":[{"instType":"spot","topic":"ticker","symbol":"BTCUSDT"}]}``
* text frame ``ping`` about every 30 seconds, expect text ``pong``
* the server drops a socket that sends no ping for 2 minutes
* a ticker push is ``action: snapshot`` and ``ts`` is the server message time
* pushes happen when the book changes, about every 200ms, which is not a delay SLA

The library protocol ping is turned off. Only the text ping keeps this socket.
One snapshot is kept. Older or future messages do not replace it.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from typing import Any

from .config import PUBLIC_WS_URL, BitgetPaperSettings
from .market import TickerSnap, _api_decimal
from .models import QuoteRejected, epoch_ms

_SUBSCRIBE = json.dumps(
    {"op": "subscribe", "args": [{"instType": "spot", "topic": "ticker", "symbol": "BTCUSDT"}]},
    separators=(",", ":"),
)
_INTERVAL_WINDOW = 30


class PublicTickerFeed:
    def __init__(self, settings: BitgetPaperSettings, *, connector=None, now_fn=None, rng=None):
        self.settings = settings
        self.url = PUBLIC_WS_URL
        self._connector = connector or _live_connector
        self._now = now_fn
        self._rng = rng or random.Random()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self.connection = "disconnected"
        self.subscription = "none"
        self.subscribe_error = ""
        self.conn_id = ""
        self.heartbeat = "idle"
        self.accepted = 0
        self.rejected = 0
        self.duplicates = 0
        self.reconnects = 0
        self.disorder = 0
        self.future = 0
        self._snap: TickerSnap | None = None
        self._last_server_ts: int | None = None
        self._ping_sent_ms: int | None = None
        self._last_pong_ms: int | None = None
        self._intervals: deque[int] = deque(maxlen=_INTERVAL_WINDOW)
        self.clock_status = "uncalibrated"
        self.clock_offset_ms: int | None = None
        self.clock_rtt_ms: int | None = None
        self.clock_uncertainty_ms: int | None = None
        self.clock_server_ms: int | None = None
        self.clock_error = ""
        self.trade_source = "none"
        self.trade_note = ""

    def latest(self) -> TickerSnap | None:
        return self._snap

    def clock_blocks_trading(self) -> bool:
        return self.clock_status == "shifted"

    def set_clock(self, status: str, offset_ms: int | None, rtt_ms: int | None, uncertainty_ms: int | None, server_ms: int | None) -> None:
        self.clock_status = status
        self.clock_offset_ms = offset_ms
        self.clock_rtt_ms = rtt_ms
        self.clock_uncertainty_ms = uncertainty_ms
        self.clock_server_ms = server_ms
        self.clock_error = ""

    def set_clock_failed(self, reason: str) -> None:
        self.clock_error = reason[:80]
        if self.clock_offset_ms is None:
            self.clock_status = "failed"

    def note_trade(self, source: str, note: str) -> None:
        self.trade_source = source
        self.trade_note = note[:80]

    def view(self) -> dict[str, Any]:
        snap = self._snap
        intervals = list(self._intervals)
        return {
            "url": self.url,
            "connection": self.connection,
            "subscription": self.subscription,
            "subscribe_error": self.subscribe_error,
            "conn_id": self.conn_id[:40],
            "heartbeat": self.heartbeat,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "reconnects": self.reconnects,
            "disorder": self.disorder,
            "future": self.future,
            "server_ts_ms": None if snap is None else snap.server_ts_ms,
            "received_at_ms": None if snap is None else snap.received_at_ms,
            "arrival_minus_server_ms": None if snap is None else snap.received_at_ms - snap.server_ts_ms,
            "last_price": None if snap is None else format(snap.last, "f"),
            "bid": None if snap is None else format(snap.bid, "f"),
            "ask": None if snap is None else format(snap.ask, "f"),
            "last_pong_ms": self._last_pong_ms,
            "clock_status": self.clock_status,
            "clock_offset_ms": self.clock_offset_ms,
            "clock_rtt_ms": self.clock_rtt_ms,
            "clock_uncertainty_ms": self.clock_uncertainty_ms,
            "clock_server_ms": self.clock_server_ms,
            "clock_error": self.clock_error,
            "interval_samples": len(intervals),
            "interval_window": _INTERVAL_WINDOW,
            "interval_last_ms": None if not intervals else intervals[-1],
            "interval_min_ms": None if not intervals else min(intervals),
            "interval_max_ms": None if not intervals else max(intervals),
            "trade_source": self.trade_source,
            "trade_note": self.trade_note,
            "enabled": self.settings.public_ws_enabled,
            "max_quote_age_seconds": self.settings.max_quote_age_seconds,
            "poll_seconds": self.settings.poll_seconds,
        }

    async def run(self, external_stop: asyncio.Event | None = None) -> None:
        self._task = asyncio.current_task()
        attempt = 0
        while not self._stop.is_set() and (external_stop is None or not external_stop.is_set()):
            try:
                self.connection = "connecting"
                self.subscription = "pending"
                self.heartbeat = "idle"
                async with self._connector(self.url, open_timeout=self.settings.timeout_seconds) as ws:
                    self._ws = ws
                    self.connection = "connected"
                    await self._session(ws, external_stop)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.subscribe_error = exc.reason if isinstance(exc, QuoteRejected) else type(exc).__name__
                if isinstance(exc, QuoteRejected) and exc.reason == "subscribe_rejected":
                    self.subscription = "error"
            finally:
                self._ws = None
                if self.connection != "stopping":
                    self.connection = "disconnected"
                if self.subscription != "error":
                    self.subscription = "none"
            if self._stop.is_set() or (external_stop is not None and external_stop.is_set()):
                break
            self.reconnects += 1
            delay = min(self.settings.ws_reconnect_max_seconds, 2 ** min(attempt, 5))
            delay = delay * (0.5 + self._rng.random() * 0.5)
            attempt += 1
            await self._sleep(delay, external_stop)
        self.connection = "disconnected"

    async def stop(self) -> None:
        self.connection = "stopping"
        self._stop.set()
        ws = self._ws
        if ws is not None:
            close = getattr(ws, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.connection = "disconnected"

    async def _session(self, ws: Any, external_stop: asyncio.Event | None) -> None:
        await ws.send(_SUBSCRIBE)
        next_ping = self._now_ms() + self.settings.ws_ping_seconds * 1000
        pong_deadline: int | None = None
        while not self._stop.is_set() and (external_stop is None or not external_stop.is_set()):
            timeout = max(0.05, min(1.0, (next_ping - self._now_ms()) / 1000))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout)
            except asyncio.TimeoutError:
                now_ms = self._now_ms()
                if pong_deadline is not None and now_ms > pong_deadline:
                    self.heartbeat = "late"
                    raise QuoteRejected("pong_timeout")
                if now_ms >= next_ping:
                    await ws.send("ping")
                    self._ping_sent_ms = now_ms
                    pong_deadline = now_ms + self.settings.ws_pong_timeout_seconds * 1000
                    next_ping = now_ms + self.settings.ws_ping_seconds * 1000
                    self.heartbeat = "waiting"
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if not isinstance(raw, str):
                self.rejected += 1
                continue
            if len(raw) > 1_000_000:
                self.rejected += 1
                continue
            if raw == "pong":
                self._last_pong_ms = self._now_ms()
                pong_deadline = None
                self.heartbeat = "ok"
                continue
            if raw == "ping":
                await ws.send("pong")
                continue
            self._on_json(raw)

    def _on_json(self, raw: str) -> None:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self.rejected += 1
            return
        if not isinstance(body, dict):
            self.rejected += 1
            return
        event = body.get("event")
        if event == "subscribe":
            self._on_subscribe(body)
            return
        if event == "error":
            self.subscription = "error"
            self.subscribe_error = str(body.get("code") or body.get("msg") or "error")[:80]
            raise QuoteRejected("subscribe_rejected")
        code = body.get("code")
        if code not in (None, "0", "00000") and body.get("action") != "snapshot":
            self.subscription = "error"
            self.subscribe_error = str(code)[:32]
            raise QuoteRejected("subscribe_rejected")
        if body.get("action") == "snapshot":
            self._on_snapshot(body)
            return
        self.rejected += 1

    def _on_subscribe(self, body: dict[str, Any]) -> None:
        arg = body.get("arg") if isinstance(body.get("arg"), dict) else {}
        if arg.get("instType") != "spot" or arg.get("topic") != "ticker" or arg.get("symbol") != "BTCUSDT":
            self.subscription = "error"
            self.subscribe_error = "unexpected_channel"
            raise QuoteRejected("subscribe_rejected")
        code = body.get("code")
        if code not in (None, "0", "00000"):
            self.subscription = "error"
            self.subscribe_error = str(code)[:32]
            raise QuoteRejected("subscribe_rejected")
        self.subscription = "subscribed"
        self.subscribe_error = ""
        conn = body.get("connId")
        self.conn_id = conn if isinstance(conn, str) else ""

    def _on_snapshot(self, body: dict[str, Any]) -> None:
        arg = body.get("arg") if isinstance(body.get("arg"), dict) else {}
        if arg.get("instType") != "spot" or arg.get("topic") != "ticker" or arg.get("symbol") != "BTCUSDT":
            self.rejected += 1
            return
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            self.rejected += 1
            return
        try:
            raw_ts = body.get("ts")
            if isinstance(raw_ts, str) and raw_ts.isdigit():
                server_ts = int(raw_ts)
            elif isinstance(raw_ts, int) and not isinstance(raw_ts, bool):
                server_ts = raw_ts
            else:
                raise QuoteRejected("bad_timestamp")
            if server_ts < 0 or len(str(server_ts)) != 13:
                raise QuoteRejected("bad_timestamp")
            row = rows[0]
            bid = _api_decimal(row.get("bid1Price"), positive=True, reason="non_positive_price")
            ask = _api_decimal(row.get("ask1Price"), positive=True, reason="non_positive_price")
            last = _api_decimal(row.get("lastPrice"), positive=True, reason="non_positive_price")
            bid_size = _api_decimal(row.get("bid1Size"), positive=True, reason="non_positive_size")
            ask_size = _api_decimal(row.get("ask1Size"), positive=True, reason="non_positive_size")
            if ask < bid:
                raise QuoteRejected("crossed_book")
        except (QuoteRejected, KeyError, TypeError, ValueError):
            self.rejected += 1
            return
        now_ms = self._now_ms()
        skew_ms = self.settings.clock_skew_seconds * 1000
        if server_ts > now_ms + skew_ms:
            self.future += 1
            self.rejected += 1
            return
        if self._last_server_ts is not None and server_ts < self._last_server_ts:
            self.disorder += 1
            self.rejected += 1
            return
        if self._last_server_ts is not None and server_ts == self._last_server_ts:
            self.duplicates += 1
            return
        if self._last_server_ts is not None:
            self._intervals.append(server_ts - self._last_server_ts)
        self._last_server_ts = server_ts
        self._snap = TickerSnap(bid, ask, last, bid_size, ask_size, server_ts, now_ms)
        self.accepted += 1

    def _now_ms(self) -> int:
        if self._now is not None:
            return epoch_ms(self._now())
        from datetime import datetime, timezone
        return epoch_ms(datetime.now(timezone.utc))

    async def _sleep(self, seconds: float, external_stop: asyncio.Event | None) -> None:
        delay = max(0.0, seconds)
        if external_stop is None:
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                return
            return
        stop_wait = asyncio.create_task(self._stop.wait())
        ext_wait = asyncio.create_task(external_stop.wait())
        try:
            await asyncio.wait({stop_wait, ext_wait}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
            ext_wait.cancel()


async def diagnose_stream(settings: BitgetPaperSettings, seconds: int) -> dict[str, Any]:
    """Read the public stream for a bounded time. Does not open the ledger or place a fill."""
    from datetime import datetime, timezone

    from .market import PublicMarketClient

    feed = PublicTickerFeed(settings)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    client = PublicMarketClient(settings)
    rest_last = ""
    rest_error = ""
    try:
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
        await asyncio.sleep(seconds)
        try:
            quote = await client.fetch(datetime.now(timezone.utc))
            rest_last = format(quote.last, "f")
        except QuoteRejected as exc:
            rest_error = exc.reason
    finally:
        stop.set()
        await feed.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    view = feed.view()
    return {
        "status": "ws_diagnose",
        "execution": "not_started",
        "fill_written": False,
        "authenticated": False,
        "seconds": seconds,
        "market": view,
        "rest_last_price": rest_last,
        "rest_error": rest_error,
        "note": "REST last price is only a check. arrival_minus_server_ms mixes clock, network and queue; it is not one-way delay.",
    }


def _live_connector(url: str, open_timeout: int):
    import websockets

    return websockets.connect(
        url,
        open_timeout=open_timeout,
        ping_interval=None,
        ping_timeout=None,
        max_size=1_000_000,
        max_queue=32,
    )
