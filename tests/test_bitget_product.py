"""Product behavior beyond the first one-shot paper book.

Candle series here are synthetic and labeled by the test names. They are not
live Bitget prints. Replay fills in this file are local simulator output.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from urllib.request import urlopen

import pytest

from src.bitget_paper.cli import main
from src.bitget_paper.config import settings_from_section
from src.bitget_paper.engine import run_cycle
from src.bitget_paper.loop import run_loop
from src.bitget_paper.models import StateError
from src.bitget_paper.status_server import StatusServer
from src.bitget_paper.store import BookLock, lock_path_for, write_control
from tests.test_bitget_paper import (
    NOW,
    NOW_MS,
    bearish_closes,
    bullish_closes,
    from_epoch_ms,
    ok_responses,
    paper_config,
    records,
    scripted_config,
)


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


async def test_sma_round_trip_buys_then_closes(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses(closes=bullish_closes()))
    opened = await run_cycle(config, transport=transport, now=NOW)
    assert opened.status == "filled"
    assert opened.summary["action"] == "BUY"
    assert opened.summary["correlation_id"]
    bought = _state(tmp_path)

    later = NOW_MS + 60_000
    closing, transport2 = scripted_config(tmp_path, ok_responses(later, closes=bearish_closes()))
    closed = await run_cycle(closing, transport=transport2, now=from_epoch_ms(later))
    assert closed.status == "filled"
    assert closed.summary["action"] == "SELL"
    assert closed.fill_written is True
    flat = _state(tmp_path)
    assert Decimal(flat["quantity_btc"]) == 0
    assert Decimal(flat["realized_pnl_usd"]) != 0
    assert flat["fill_count"] == bought["fill_count"] + 1
    assert opened.summary["correlation_id"] != closed.summary["correlation_id"]
    fills = [row for row in records(tmp_path) if row["type"] == "bitget_paper_fill"]
    assert [row["side"] for row in fills] == ["BUY", "SELL"]
    assert "NOT A LIVE FILL" not in json.dumps(fills)
    assert all(row["not_exchange_order"] is True for row in fills)


async def test_restart_does_not_buy_twice_on_the_same_signal(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    first = await run_cycle(config, transport=transport, now=NOW)
    assert first.fill_written is True
    qty = _state(tmp_path)["quantity_btc"]
    again, transport2 = scripted_config(tmp_path, ok_responses(NOW_MS + 1_000))
    second = await run_cycle(again, transport=transport2, now=from_epoch_ms(NOW_MS + 1_000))
    assert second.fill_written is False
    assert second.status == "held"
    assert _state(tmp_path)["quantity_btc"] == qty
    assert _state(tmp_path)["fill_count"] == 1


async def test_duplicate_bar_does_not_fill_again_if_the_book_was_reset(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    await run_cycle(config, transport=transport, now=NOW)
    path = tmp_path / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["quantity_btc"] = "0"
    raw["cost_basis_usd"] = "0"
    raw["cash_usd"] = "100"
    path.write_text(json.dumps(raw), encoding="utf-8")
    again, transport2 = scripted_config(tmp_path, ok_responses())
    result = await run_cycle(again, transport=transport2, now=NOW)
    assert result.fill_written is False
    assert result.reason == "duplicate_bar"


async def test_pause_blocks_buys_and_emergency_stop_does_not_sell(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    write_control(tmp_path / "control.json", "paused")
    paused = await run_cycle(config, transport=transport, now=NOW)
    assert paused.reason == "paused"
    assert paused.fill_written is False
    assert not (tmp_path / "state.json").exists() or _state(tmp_path)["fill_count"] == 0

    write_control(tmp_path / "control.json", "running")
    opened, transport2 = scripted_config(tmp_path, ok_responses())
    assert (await run_cycle(opened, transport=transport2, now=NOW)).fill_written is True
    write_control(tmp_path / "control.json", "emergency_stop")
    later = NOW_MS + 60_000
    held, transport3 = scripted_config(tmp_path, ok_responses(later, closes=bearish_closes()))
    stopped = await run_cycle(held, transport=transport3, now=from_epoch_ms(later))
    assert stopped.reason == "emergency_stop_holds_position"
    assert Decimal(_state(tmp_path)["quantity_btc"]) > 0


async def test_emergency_flatten_is_opt_in(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses(), emergency_flatten=True)
    assert (await run_cycle(config, transport=transport, now=NOW)).fill_written is True
    write_control(tmp_path / "control.json", "emergency_stop")
    later = NOW_MS + 60_000
    closing, transport2 = scripted_config(
        tmp_path, ok_responses(later, closes=bearish_closes()), emergency_flatten=True
    )
    result = await run_cycle(closing, transport=transport2, now=from_epoch_ms(later))
    assert result.fill_written is True
    assert result.summary["action"] == "SELL"
    assert Decimal(_state(tmp_path)["quantity_btc"]) == 0
    assert _state(tmp_path)["emergency_flattened"] is True


async def test_drawdown_rejects_a_new_buy(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses(), max_drawdown_pct="0.10")
    from src.bitget_paper.engine import fresh_state, save_state
    from src.bitget_paper.config import settings_from_root

    settings = settings_from_root(config)
    state = fresh_state(settings, NOW).model_copy(
        update={"cash_usd": Decimal("50"), "peak_equity_usd": Decimal("100")}
    )
    save_state(tmp_path / "state.json", state)
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.reason == "max_drawdown"
    assert result.fill_written is False


async def test_second_process_cannot_write_the_same_book(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    lock = BookLock(lock_path_for(tmp_path / "state.json"))
    lock.acquire()
    try:
        with pytest.raises(StateError) as exc:
            await run_cycle(config, transport=transport, now=NOW)
        assert exc.value.reason == "already_running"
    finally:
        lock.release()
    assert not any(row.get("type") == "bitget_paper_fill" for row in records(tmp_path))


async def test_v1_state_migrates_and_unknown_version_is_left_alone(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    legacy = {
        "version": 1,
        "label": "LOCAL_PAPER_DEMO",
        "symbol": "BTCUSDT",
        "category": "SPOT",
        "cash_usd": "100",
        "quantity_btc": "0",
        "cost_basis_usd": "0",
        "realized_pnl_usd": "0",
        "fees_paid_usd": "0",
        "fill_count": 0,
        "last_mark_price": None,
        "last_unrealized_pnl_usd": "0",
        "updated_at": "2026-11-19T00:00:00Z",
    }
    (tmp_path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.fill_written is True
    assert _state(tmp_path)["version"] == 2

    bad = tmp_path / "state.json"
    bad.write_text(json.dumps({"version": 99, "label": "LOCAL_PAPER_DEMO"}), encoding="utf-8")
    with pytest.raises(StateError) as exc:
        await run_cycle(config, transport=transport, now=NOW)
    assert exc.value.reason == "state_version_unsupported"
    assert json.loads(bad.read_text())["version"] == 99


async def test_shutdown_before_the_fill_writes_no_trade(tmp_path):
    import asyncio

    config, transport = scripted_config(tmp_path, ok_responses())
    stop = asyncio.Event()
    stop.set()
    result = await run_cycle(config, transport=transport, now=NOW, stop_event=stop)
    assert result.fill_written is False
    assert result.reason == "shutdown"


async def test_run_loop_persists_two_cycles_and_stops(tmp_path):
    responses = ok_responses() + ok_responses(NOW_MS + 1_000)

    class Sequence:
        def __init__(self):
            self.calls = []
            self._items = list(responses)

        async def __call__(self, method, url, params, headers):
            self.calls.append(url)
            item = self._items.pop(0)
            return item

    async def instant(_seconds):
        return None

    config = paper_config(tmp_path, status_enabled=False)
    summary = await run_loop(
        config,
        transport=Sequence(),
        max_cycles=2,
        sleep=instant,
        now_fn=lambda: NOW,
    )
    assert summary["cycles"] == 2
    assert _state(tmp_path)["fill_count"] == 1
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["runtime"] == "stopped"
    assert status["banner"] == "PAPER ONLY"
    assert status["authenticated"] is False


def test_status_page_is_loopback_and_read_only():
    snapshot = {
        "banner": "PAPER ONLY",
        "banner_zh": "本地纸面",
        "runtime": "running",
        "authenticated": False,
        "exchange_order": False,
        "strategy_expects_profit": False,
        "feed": {"status": "ok", "reason": "baseline_keep_long", "ticker_age_ms": 10},
        "strategy": {"id": "baseline_sma_cross_v1", "fast_period": 5, "slow_period": 20, "interval": "1m"},
        "risk": {
            "max_position_usd": "100",
            "max_loss_usd": "100",
            "max_drawdown_pct": "0.25",
            "emergency_flatten": False,
        },
        "book": {
            "cash_usd": "74",
            "quantity_btc": "0.0004",
            "realized_usd": "0",
            "unrealized_usd": "-0.07",
            "equity_usd": "99",
        },
        "last_decision": {"action": "HOLD", "reason": "baseline_keep_long", "correlation_id": "c000001abcd1234"},
        "recent_decisions": [
            {"ts": "2026-11-20T00:00:00+00:00", "action": "HOLD", "reason": "baseline_keep_long", "correlation_id": "c000001abcd1234"}
        ],
        "equity": [
            {"ts": "2026-11-20T00:00:00+00:00", "equity_usd": "99", "cash_usd": "74", "realized_usd": "0", "unrealized_usd": "-0.07"}
        ],
    }
    server = StatusServer("127.0.0.1", 0, lambda: snapshot)
    server.start()
    try:
        port = server.bound_port
        with urlopen(f"http://127.0.0.1:{port}/health") as response:
            body = json.loads(response.read().decode())
        assert body["banner"] == "PAPER ONLY"
        assert body["authenticated"] is False
        with urlopen(f"http://127.0.0.1:{port}/") as response:
            page = response.read().decode()
        assert "PAPER ONLY" in page
        assert "不是交易所订单" in page
        assert "紧急停止默认不平仓" in page
        assert "baseline_keep_long" in page
    finally:
        server.stop()
    with pytest.raises(ValueError):
        StatusServer("0.0.0.0", 0, lambda: snapshot)


def test_public_host_and_fast_period_are_rejected():
    with pytest.raises(Exception) as exc:
        settings_from_section({"status_host": "0.0.0.0"})
    assert "live" not in exc.value.reason
    assert exc.value.reason.startswith("invalid_bitget_paper")
    with pytest.raises(Exception):
        settings_from_section({"fast_period": 20, "slow_period": 20})


def test_cli_control_does_not_trade(tmp_path, capsys):
    config = paper_config(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(__import__("yaml").safe_dump(config), encoding="utf-8")
    assert main(["--config", str(path), "--control", "emergency-stop"]) == 0
    assert json.loads((tmp_path / "control.json").read_text())["mode"] == "emergency_stop"
    assert not (tmp_path / "state.json").exists()
    assert "control_written" in capsys.readouterr().out


async def test_stale_candles_do_not_fill(tmp_path):
    from tests.test_bitget_paper import Script, dump, freshen, FIXTURE

    old = NOW_MS - 30 * 60_000
    instruments = dump(freshen(FIXTURE["instruments_body"], NOW_MS))
    tickers = dump(freshen(FIXTURE["tickers_body"], NOW_MS))
    from tests.test_bitget_paper import candle_payload

    candles = dump(candle_payload(old, bullish_closes()))
    # requestTime on that helper is `old`, which is stale versus NOW.
    result = await run_cycle(
        paper_config(tmp_path),
        transport=Script([(200, instruments), (200, tickers), (200, candles)]),
        now=NOW,
    )
    assert result.fill_written is False
    assert result.reason in {"stale_candles", "stale_quote", "future_quote"}
