"""Public ticker stream behavior with scripted sockets. No network."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from src.bitget_paper.config import settings_from_section
from src.bitget_paper.engine import run_cycle
from src.bitget_paper.market import (
    ClockSample,
    TickerSnap,
    clock_sample_from_rtt,
    judge_clock,
    parse_server_time,
)
from src.bitget_paper.models import QuoteRejected
from src.bitget_paper.loop import _copy_status
from src.bitget_paper.public_ws import PublicTickerFeed
from src.bitget_paper.status_server import _page
from src.bitget_paper.engine import fresh_state, load_state, save_state
from tests.test_bitget_paper import (
    NOW,
    NOW_MS,
    Script,
    bullish_closes,
    candle_payload,
    dump,
    fresh_payloads,
    from_epoch_ms,
    paper_config,
)


class _Sock:
    def __init__(self, items: list[object]):
        self.sent: list[str] = []
        self.closed = False
        self._items = asyncio.Queue()
        for item in items:
            self._items.put_nowait(item)

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if data == "ping":
            await self._items.put("pong")

    async def recv(self):
        item = await self._items.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    async def close(self) -> None:
        self.closed = True
        await self._items.put(None)


class _CM:
    def __init__(self, sock: _Sock):
        self.sock = sock

    async def __aenter__(self):
        return self.sock

    async def __aexit__(self, exc_type, exc, tb):
        await self.sock.close()
        return False


def _settings(tmp_path, **overrides):
    from src.bitget_paper.config import settings_from_root

    return settings_from_root(paper_config(tmp_path, **overrides))


def _snap(ts: int = NOW_MS) -> TickerSnap:
    _, ticker_text = fresh_payloads(ts)
    row = json.loads(ticker_text)["data"][0]
    return TickerSnap(
        Decimal(row["bid1Price"]),
        Decimal(row["ask1Price"]),
        Decimal(row["lastPrice"]),
        Decimal(row["bid1Size"]),
        Decimal(row["ask1Size"]),
        ts,
        ts,
    )


def _snapshot(ts: int, price: str = "100") -> str:
    return json.dumps(
        {
            "action": "snapshot",
            "arg": {"instType": "spot", "topic": "ticker", "symbol": "BTCUSDT"},
            "ts": ts,
            "data": [{
                "bid1Price": price,
                "ask1Price": price,
                "lastPrice": price,
                "bid1Size": "1",
                "ask1Size": "1",
            }],
        }
    )


_ACK = json.dumps({"event": "subscribe", "arg": {"instType": "spot", "topic": "ticker", "symbol": "BTCUSDT"}, "connId": "c1"})


async def _run_feed(tmp_path, sock: _Sock, **overrides) -> PublicTickerFeed:
    settings = _settings(tmp_path, ws_ping_seconds=5, ws_pong_timeout_seconds=2, **overrides)
    feed = PublicTickerFeed(
        settings,
        connector=lambda url, open_timeout: _CM(sock),
        now_fn=lambda: from_epoch_ms(NOW_MS),
    )
    task = asyncio.create_task(feed.run())
    for _ in range(50):
        if feed.accepted or feed.subscription == "error" or feed.rejected:
            break
        await asyncio.sleep(0.02)
    await feed.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return feed


async def test_subscribe_snapshot_and_stop(tmp_path):
    sock = _Sock([_ACK, _snapshot(NOW_MS)])
    feed = await _run_feed(tmp_path, sock)
    assert feed.accepted == 1
    assert feed.subscription in {"subscribed", "none"}
    assert sock.closed is True
    assert feed.latest() is not None
    assert any(item.startswith('{"op":"subscribe"') for item in sock.sent)


async def test_malformed_disorder_and_future_do_not_replace_a_good_snap(tmp_path):
    good = _snapshot(NOW_MS, "100")
    older = _snapshot(NOW_MS - 1000, "90")
    future = _snapshot(NOW_MS + 10_000, "1")
    sock = _Sock([_ACK, "{", good, older, future, _snapshot(NOW_MS, "0")])
    feed = await _run_feed(tmp_path, sock)
    assert feed.latest() is not None
    assert feed.latest().last == Decimal("100")
    assert feed.rejected >= 3
    assert feed.disorder >= 1
    assert feed.future >= 1


async def test_reconnects_after_a_dropped_socket(tmp_path):
    sock = _Sock([_ACK, _snapshot(NOW_MS)])
    state = {"n": 0}

    class Boom:
        async def __aenter__(self):
            raise ConnectionError("down")

        async def __aexit__(self, *args):
            return False

    def connector(url, open_timeout):
        state["n"] += 1
        if state["n"] == 1:
            return Boom()
        return _CM(sock)

    settings = _settings(tmp_path, ws_reconnect_max_seconds=1, ws_ping_seconds=30, ws_pong_timeout_seconds=5)
    feed = PublicTickerFeed(settings, connector=connector, now_fn=lambda: from_epoch_ms(NOW_MS), rng=__import__("random").Random(1))
    task = asyncio.create_task(feed.run())
    for _ in range(80):
        if feed.accepted:
            break
        await asyncio.sleep(0.05)
    await feed.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert feed.reconnects >= 1
    assert feed.accepted == 1
    assert sock.closed is True


async def test_ping_pong_marks_heartbeat(tmp_path):
    sock = _Sock([_ACK, _snapshot(NOW_MS)])
    settings = _settings(tmp_path, ws_ping_seconds=5, ws_pong_timeout_seconds=2)
    feed = PublicTickerFeed(settings, connector=lambda url, open_timeout: _CM(sock))
    task = asyncio.create_task(feed.run())
    seen = False
    for _ in range(40):
        if "ping" in sock.sent and feed.heartbeat == "ok":
            seen = True
            break
        await asyncio.sleep(0.25)
    await feed.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert seen is True


def test_clock_offset_keeps_the_sign_and_names_a_large_shift(tmp_path):
    sample = clock_sample_from_rtt(1_000, 1_200, 900)
    assert sample.offset_ms == -200
    assert sample.rtt_ms == 200
    assert sample.uncertainty_ms == 100
    settings = _settings(tmp_path)
    assert judge_clock(sample, settings) == "ok"
    noisy = clock_sample_from_rtt(1_000, 1_400, 1_100)
    assert noisy.offset_ms == -100
    assert judge_clock(noisy, settings) == "within_noise"
    shifted = ClockSample("ok", 10_000, 100, 50, NOW_MS, NOW_MS)
    assert judge_clock(shifted, settings) == "shifted"
    body = '{"code":"00000","msg":"success","requestTime":1795595160091,"data":{"serverTime":"1795595160091","serverTimeMicros":"1795595160091832"}}'
    assert parse_server_time(200, body) == 1795595160091
    with pytest.raises(QuoteRejected):
        parse_server_time(200, '{"code":"00000","data":{"serverTime":"12"}}')


def test_old_config_enables_ws_without_resetting_risk():
    settings = settings_from_section({"max_position_usd": "90.00"})
    assert settings.public_ws_enabled is True
    assert settings.max_position_usd == Decimal("90.00")
    assert settings.demo_cash_usd == Decimal("100.00")
    assert settings.strategy == "baseline_sma_cross_v1"
    assert settings.emergency_flatten is False
    assert settings.max_loss_usd == Decimal("100.00")


def test_status_copy_reads_the_current_stream_view():
    import threading

    class Feed:
        def view(self):
            return {"connection": "connected", "subscription": "subscribed", "accepted": 4}

    published = {"market": {"accepted": 0}}
    body = _copy_status(published, threading.Lock(), Feed())
    assert body["market"]["accepted"] == 4
    assert body["market"]["subscription"] == "subscribed"
    assert isinstance(body["served_at_ms"], int)


def test_status_page_shows_stream_facts_without_a_fake_percentile():
    page = _page({
        "runtime": "running",
        "feed": {"status": "rejected", "reason": "stale_quote"},
        "book": {"cash_usd": "70", "quantity_btc": "0.0004", "equity_usd": "99", "stale": True},
        "strategy": {},
        "risk": {"emergency_flatten": False},
        "market": {
            "connection": "disconnected",
            "subscription": "none",
            "heartbeat": "ok",
            "trade_source": "none",
            "clock_status": "uncalibrated",
            "arrival_minus_server_ms": -25,
            "interval_samples": 0,
            "accepted": 0,
            "reconnects": 2,
        },
        "served_at_ms": NOW_MS,
    })
    assert "尚未订阅" in page
    assert "未校准" in page
    assert "-25" in page
    assert "样本不足，不计算分位数" in page
    assert "p95" not in page
    assert "0.0004" in page
    assert "PAPER ONLY" in page


class _Feed:
    def __init__(self, snap, shifted: bool = False):
        self._snap = snap
        self.shifted = shifted
        self.notes: list[tuple[str, str]] = []

    def latest(self):
        return self._snap

    def clock_blocks_trading(self):
        return self.shifted

    def note_trade(self, source: str, note: str) -> None:
        self.notes.append((source, note))


async def test_fresh_ws_snapshot_is_used_and_rest_ticker_is_not(tmp_path):
    instruments, _ticker = fresh_payloads(NOW_MS)
    candles = dump(candle_payload(NOW_MS, bullish_closes()))
    script = Script([(200, instruments), (200, candles)])
    config = paper_config(tmp_path)
    feed = _Feed(_snap(NOW_MS))
    result = await run_cycle(config, transport=script, now=NOW, ticker_feed=feed)
    assert result.summary["quote_source"] == "bitget_public_ws_v3"
    assert feed.notes[-1][0] == "bitget_public_ws_v3"
    assert all("tickers" not in call["url"] for call in script.calls)


async def test_stale_ws_falls_back_to_fresh_rest(tmp_path):
    instruments, tickers = fresh_payloads(NOW_MS)
    candles = dump(candle_payload(NOW_MS, bullish_closes()))
    script = Script([(200, instruments), (200, instruments), (200, tickers), (200, candles)])
    feed = _Feed(_snap(NOW_MS - 30_000))
    result = await run_cycle(paper_config(tmp_path), transport=script, now=NOW, ticker_feed=feed)
    assert result.summary["quote_source"] == "bitget_public_rest_v3"
    assert feed.notes[-1][1] == "stale_quote"


async def test_clock_shift_and_stale_rest_do_not_clear_a_position(tmp_path):
    config = paper_config(tmp_path)
    state = fresh_state(_settings(tmp_path), NOW).model_copy(
        update={"quantity_btc": Decimal("0.001"), "cost_basis_usd": Decimal("50")}
    )
    save_state(tmp_path / "state.json", state)
    instruments, tickers = fresh_payloads(NOW_MS - 60_000)
    script = Script([(200, instruments), (200, tickers)])
    feed = _Feed(None, shifted=True)
    result = await run_cycle(config, transport=script, now=NOW, ticker_feed=feed)
    assert result.status == "quote_rejected"
    assert result.reason == "clock_anomaly"
    assert result.fill_written is False
    kept = load_state(tmp_path / "state.json", _settings(tmp_path), NOW)
    assert kept.quantity_btc == Decimal("0.001")
    assert script.calls == []
