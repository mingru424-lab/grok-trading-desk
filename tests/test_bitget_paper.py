"""Bitget public-quote paper book.

The JSON fixture under tests/fixtures is a frozen, non-realtime snapshot.
Tests that need a usable quote rewrite its timestamps to the injected clock.
Nothing in this module sends an API key or constructs an exchange executor.
"""

from __future__ import annotations

import ast
import copy
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from src.bitget_paper.broker import simulate_buy, simulate_sell_all
from src.bitget_paper.cli import main
from src.bitget_paper.config import settings_from_root, settings_from_section
from src.bitget_paper.decision import demo_decide
from src.bitget_paper.engine import fresh_state, run_cycle, save_state
from src.bitget_paper.market import PUBLIC_HEADERS, parse_public_book
from src.bitget_paper.models import (
    DECISION_SCHEMA,
    ContractError,
    DemoDecision,
    QuoteRejected,
    ValidatedQuote,
    conforms,
    epoch_ms,
    from_epoch_ms,
)
from src.bitget_paper.risk import PaperRiskGate

FIXTURE_PATH = Path("tests/fixtures/bitget_btcusdt_public_snapshot.json")
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
NOW_MS = 1_795_056_776_682
NOW = from_epoch_ms(NOW_MS)
CANARIES = (
    "CANARY_SECRET_DO_NOT_LEAK",
    "CANARY_WALLET_DO_NOT_LEAK",
    "CANARY_GROK_DO_NOT_LEAK",
    "CANARY_ENV_DO_NOT_LEAK",
)


def paper_config(tmp_path: Path, **overrides) -> dict:
    root = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    section = root["bitget_paper"]
    section["log_path"] = str(tmp_path / "paper.jsonl")
    section["state_path"] = str(tmp_path / "state.json")
    section.update(overrides)
    root["bitget_paper"] = section
    return root


def freshen(body: dict, now_ms: int, *, request_delta: int = 0, row_delta: int = 0) -> dict:
    cloned = copy.deepcopy(body)
    cloned["requestTime"] = now_ms + request_delta
    row = cloned["data"][0]
    if "ts" in row:
        row["ts"] = str(now_ms + row_delta)
    return cloned


def dump(body: dict) -> str:
    return json.dumps(body)


def fresh_payloads(now_ms: int = NOW_MS, **edits) -> tuple[str, str]:
    instruments = freshen(FIXTURE["instruments_body"], now_ms)
    tickers = freshen(FIXTURE["tickers_body"], now_ms)
    instrument_row = instruments["data"][0]
    ticker_row = tickers["data"][0]
    for key, value in edits.items():
        if key.startswith("ticker_"):
            ticker_row[key.removeprefix("ticker_")] = value
        elif key.startswith("instrument_"):
            instrument_row[key.removeprefix("instrument_")] = value
        elif key == "instrument_code":
            instruments["code"] = value
        elif key == "ticker_code":
            tickers["code"] = value
        else:
            raise AssertionError(key)
    return dump(instruments), dump(tickers)


class Script:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, method, url, params, headers):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params), "headers": dict(headers)}
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def scripted_config(tmp_path: Path, responses, **overrides):
    return paper_config(tmp_path, **overrides), Script(responses)


def ok_responses(now_ms: int = NOW_MS, **edits):
    instruments, tickers = fresh_payloads(now_ms, **edits)
    return [(200, instruments), (200, tickers)]


def records(tmp_path: Path) -> list[dict]:
    path = tmp_path / "paper.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def simple_quote(**overrides) -> ValidatedQuote:
    payload = dict(
        symbol="BTCUSDT",
        category="SPOT",
        bid=Decimal("100"),
        ask=Decimal("100"),
        last=Decimal("100"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        ticker_ts_ms=NOW_MS,
        ticker_request_time_ms=NOW_MS,
        instrument_request_time_ms=NOW_MS,
        price_precision=2,
        quantity_precision=6,
        quote_precision=8,
        min_order_amount=Decimal("1"),
    )
    payload.update(overrides)
    return ValidatedQuote(**payload)


def settings(tmp_path: Path, **overrides):
    return settings_from_root(paper_config(tmp_path, **overrides))


# --- fixture is not a live quote -------------------------------------------------


def test_fixture_file_is_marked_non_realtime():
    assert FIXTURE["fixture_kind"] == "recorded_non_realtime"
    assert FIXTURE["realtime"] is False
    assert FIXTURE["tradable"] is False
    assert FIXTURE["do_not_use_as_a_mark"] is True
    assert FIXTURE["authenticated"] is False
    text = FIXTURE_PATH.read_text(encoding="utf-8")
    assert "Frozen public snapshot" in text


def test_recorded_snapshot_is_rejected_once_the_clock_moves():
    ts = int(FIXTURE["tickers_body"]["data"][0]["ts"])
    cfg = settings_from_section({})
    with pytest.raises(QuoteRejected) as exc:
        parse_public_book(
            200,
            dump(FIXTURE["instruments_body"]),
            200,
            dump(FIXTURE["tickers_body"]),
            from_epoch_ms(ts + 16_000),
            cfg,
        )
    assert exc.value.reason == "stale_quote"


def test_recorded_snapshot_is_not_tradable_at_the_wall_clock():
    cfg = settings_from_section({})
    with pytest.raises(QuoteRejected) as exc:
        parse_public_book(
            200,
            dump(FIXTURE["instruments_body"]),
            200,
            dump(FIXTURE["tickers_body"]),
            datetime.now(timezone.utc),
            cfg,
        )
    assert exc.value.reason in {"stale_quote", "future_quote"}


def test_quote_age_boundary():
    cfg = settings_from_section({"max_quote_age_seconds": 15, "clock_skew_seconds": 2})
    instruments, tickers = fresh_payloads(NOW_MS)
    parse_public_book(200, instruments, 200, tickers, from_epoch_ms(NOW_MS + 15_000), cfg)
    stale = freshen(FIXTURE["tickers_body"], NOW_MS, row_delta=0)
    stale["requestTime"] = NOW_MS
    with pytest.raises(QuoteRejected) as exc:
        parse_public_book(
            200,
            instruments,
            200,
            dump(stale),
            from_epoch_ms(NOW_MS + 15_001),
            cfg,
        )
    assert exc.value.reason == "stale_quote"
    future = freshen(FIXTURE["tickers_body"], NOW_MS, row_delta=2_001)
    future["requestTime"] = NOW_MS + 2_001
    instruments_future = freshen(FIXTURE["instruments_body"], NOW_MS, request_delta=2_001)
    with pytest.raises(QuoteRejected) as exc:
        parse_public_book(
            200,
            dump(instruments_future),
            200,
            dump(future),
            NOW,
            cfg,
        )
    assert exc.value.reason == "future_quote"


# --- normal local fill ------------------------------------------------------------


async def test_fresh_book_simulates_one_demo_buy(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.exit_code == 0
    assert result.status == "filled"
    assert result.fill_written is True
    assert result.summary["authenticated"] is False
    assert result.summary["exchange_order"] is False
    assert result.summary["strategy_expects_profit"] is False
    assert result.summary["execution"] == "simulate"
    assert Decimal(result.summary["unrealized_usd"]) < 0

    assert [call["method"] for call in transport.calls] == ["GET", "GET"]
    assert transport.calls[0]["url"].endswith("/api/v3/market/instruments")
    assert transport.calls[1]["url"].endswith("/api/v3/market/tickers")
    for call in transport.calls:
        assert call["headers"] == PUBLIC_HEADERS
        assert call["params"] == {"category": "SPOT", "symbol": "BTCUSDT"}
        assert "ACCESS-KEY" not in call["headers"]
        assert "Authorization" not in call["headers"]

    kinds = [row["type"] for row in records(tmp_path)]
    assert kinds.count("bitget_paper_fill") == 1
    fill = next(row for row in records(tmp_path) if row["type"] == "bitget_paper_fill")
    assert fill["label"] == "LOCAL_SIMULATED_FILL"
    assert fill["new_fill"] is True
    assert fill["not_exchange_order"] is True
    assert fill["authenticated"] is False
    assert fill["demo"] is True
    assert fill["decision"]["label"] == "DETERMINISTIC_DEMO"
    assert fill["decision"]["not_llm"] is True
    assert fill["decision"]["not_multi_agent_verification"] is True
    assert fill["decision"]["basis"]["price_used_as_signal"] is False
    assert fill["assumptions"]["fee_bps"] == 10
    assert fill["assumptions"]["slippage_bps"] == 5
    assert fill["assumptions"]["fee_is_exchange_schedule"] is False
    assert fill["assumptions"]["slippage_is_measured_fill"] is False
    assert fill["source"]["transport"] == "public_rest_unauthenticated"
    assert fill["source"]["ticker_ts_ms"] == NOW_MS
    assert "ts" in fill
    assert Decimal(fill["fill"]["price"]) > Decimal(fill["quote"]["ask"])
    assert Decimal(fill["fill"]["fee_usd"]) > 0
    assert Decimal(fill["pnl"]["unrealized_usd"]) < 0
    assert fill["pnl"]["mark_side"] == "bid_after_adverse_slippage_and_fee"
    assert Decimal(fill["position"]["quantity_btc"]) > 0

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["label"] == "LOCAL_PAPER_DEMO"
    assert state["fill_count"] == 1
    assert Decimal(state["quantity_btc"]) == Decimal(fill["position"]["quantity_btc"])


async def test_second_cycle_holds_and_does_not_fill_again(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses())
    first = await run_cycle(config, transport=transport, now=NOW)
    assert first.fill_written is True
    again, transport2 = scripted_config(tmp_path, ok_responses(NOW_MS + 1_000))
    # keep the state written by the first cycle; scripted_config rebuilt paths
    # to the same tmp_path, so the state file is still there.
    second = await run_cycle(again, transport=transport2, now=from_epoch_ms(NOW_MS + 1_000))
    assert second.status == "held"
    assert second.fill_written is False
    assert second.summary["reason"] == "demo_position_already_open_no_exit_rule"
    fills = [row for row in records(tmp_path) if row["type"] == "bitget_paper_fill"]
    assert len(fills) == 1
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["fill_count"] == 1


def test_demo_decision_does_not_read_a_price(tmp_path):
    state = fresh_state(settings(tmp_path), NOW)
    buy = demo_decide(state, True, Decimal("25"))
    assert buy.action == "BUY"
    assert buy.basis.price_used_as_signal is False
    assert buy.not_llm is True
    conforms(buy.model_dump(mode="json"), DECISION_SCHEMA)
    open_book = state.model_copy(
        update={"quantity_btc": Decimal("1"), "cost_basis_usd": Decimal("1"), "fill_count": 1}
    )
    assert demo_decide(open_book, True, Decimal("25")).action == "HOLD"
    assert demo_decide(state, False, Decimal("25")).action == "NO_TRADE"


def test_decision_schema_rejects_an_extra_return_field():
    payload = demo_decide(fresh_state(settings_from_section({}), NOW), True, Decimal("25"))
    dumped = payload.model_dump(mode="json")
    dumped["expected_return"] = "0.2"
    with pytest.raises(ContractError):
        conforms(dumped, DECISION_SCHEMA)
    with pytest.raises(ValueError):
        DemoDecision.model_validate(dumped)


def test_fee_and_slippage_round_against_the_demo_book(tmp_path):
    state = fresh_state(settings(tmp_path), NOW)
    bought = simulate_buy(state, simple_quote(), Decimal("25"), 10, 5, NOW)
    debit = state.cash_usd - bought.cash_usd
    fee = bought.fees_paid_usd
    assert bought.quantity_btc == Decimal("0.249875")
    assert fee == Decimal("0.025")
    assert debit == Decimal("25.02499375")
    assert bought.cost_basis_usd == debit


def test_sell_accounting_realizes_pnl_without_being_the_default_policy(tmp_path):
    state = fresh_state(settings(tmp_path), NOW).model_copy(
        update={
            "quantity_btc": Decimal("0.25"),
            "cost_basis_usd": Decimal("25"),
            "cash_usd": Decimal("75"),
            "fill_count": 1,
        }
    )
    sold = simulate_sell_all(state, simple_quote(bid=Decimal("110"), ask=Decimal("110")), 0, 0, NOW)
    assert sold.quantity_btc == 0
    assert sold.cost_basis_usd == 0
    assert sold.realized_pnl_usd == Decimal("2.5")
    assert sold.cash_usd == Decimal("102.5")


# --- bad quotes do not fill -------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda i, t: (500, i, 200, t), "http_status"),
        (lambda i, t: (401, i, 200, t), "http_status"),
        (lambda i, t: (200, "{", 200, t), "malformed_body"),
        (lambda i, t: (200, i, 200, json.dumps({"code": "40001", "data": []})), "api_code"),
        (lambda i, t: (200, i, 200, json.dumps({"code": "00000", "requestTime": NOW_MS, "data": []})), "unexpected_row_count"),
        (lambda i, t: (200, i, 200, json.dumps({"code": "00000", "requestTime": NOW_MS, "data": None})), "empty_data"),
    ],
)
async def test_bad_envelopes_do_not_fill(tmp_path, mutate, reason):
    instruments, tickers = fresh_payloads()
    responses = mutate(instruments, tickers)
    # mutate returns a 4-tuple of the two calls only when the first call fails
    # early. Build the script from that shape.
    if isinstance(responses[0], int):
        script = Script([(responses[0], responses[1]), (responses[2], responses[3])])
    else:
        raise AssertionError
    config = paper_config(tmp_path)
    result = await run_cycle(config, transport=script, now=NOW)
    assert result.exit_code == 3
    assert result.reason == reason
    assert result.fill_written is False
    assert not (tmp_path / "state.json").exists()
    assert all(row.get("new_fill") is False for row in records(tmp_path))
    assert not any(row["type"] == "bitget_paper_fill" for row in records(tmp_path))


async def test_disconnect_does_not_fill(tmp_path):
    script = Script([httpx.ConnectError("down")])
    result = await run_cycle(paper_config(tmp_path), transport=script, now=NOW)
    assert result.exit_code == 3
    assert result.reason == "transport_error"
    assert result.fill_written is False
    assert not (tmp_path / "state.json").exists()
    rejected = records(tmp_path)[0]
    assert rejected["detail"]["error_type"] == "ConnectError"
    assert rejected["new_fill"] is False


@pytest.mark.parametrize(
    ("edits", "reason"),
    [
        ({"ticker_symbol": "ETHUSDT"}, "symbol_mismatch"),
        ({"ticker_category": "USDT-FUTURES"}, "category_mismatch"),
        ({"ticker_lastPrice": "0"}, "non_positive_price"),
        ({"ticker_bid1Price": "-1"}, "bad_decimal"),
        ({"ticker_ask1Price": "abc"}, "bad_decimal"),
        ({"ticker_bid1Price": "70000"}, "crossed_book"),
        ({"ticker_ask1Size": "0"}, "non_positive_size"),
        ({"instrument_status": "offline"}, "instrument_not_online"),
        ({"instrument_isReality": "yes"}, "reality_token_out_of_scope"),
        ({"instrument_instrumentType": "stock"}, "not_spot_crypto"),
        ({"instrument_maintainTime": "soon"}, "instrument_maintenance"),
        ({"ticker_lastPrice": "90000"}, "last_outside_book"),
        ({"ticker_ask1Price": "80000", "ticker_lastPrice": "71320"}, "spread_too_wide"),
    ],
)
async def test_abnormal_books_do_not_fill(tmp_path, edits, reason):
    config, transport = scripted_config(tmp_path, ok_responses(**edits))
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.reason == reason
    assert result.fill_written is False
    assert not (tmp_path / "state.json").exists()


async def test_stale_ticker_does_not_fill_or_create_a_book(tmp_path):
    instruments = dump(freshen(FIXTURE["instruments_body"], NOW_MS))
    tickers = dump(freshen(FIXTURE["tickers_body"], NOW_MS, row_delta=-20_000, request_delta=-20_000))
    script = Script([(200, instruments), (200, tickers)])
    result = await run_cycle(paper_config(tmp_path), transport=script, now=NOW)
    assert result.reason == "stale_quote"
    assert result.fill_written is False
    assert not (tmp_path / "state.json").exists()
    decision = next(row for row in records(tmp_path) if row["type"] == "bitget_paper_decision")
    assert decision["decision"]["action"] == "NO_TRADE"
    assert decision["new_fill"] is False


async def test_top_of_book_smaller_than_the_demo_order_does_not_fill(tmp_path):
    config, transport = scripted_config(tmp_path, ok_responses(ticker_ask1Size="0.00000001"))
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.status == "risk_rejected"
    assert result.reason == "insufficient_top_of_book"
    assert result.fill_written is False
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["fill_count"] == 0
    assert Decimal(state["quantity_btc"]) == 0


# --- risk -------------------------------------------------------------------------


def test_loss_limit_and_position_cap_reject_instead_of_resizing():
    gate = PaperRiskGate(Decimal("100"), Decimal("100"))
    blocked, reason = gate.approve_buy(
        exposure_usd=Decimal("0"),
        realized_pnl_usd=Decimal("-100"),
        unrealized_pnl_usd=Decimal("0"),
        buy_notional_usd=Decimal("25"),
        fee_bps=10,
    )
    assert blocked is False and reason == "max_loss_limit"

    room, reason = gate.approve_buy(
        exposure_usd=Decimal("0"),
        realized_pnl_usd=Decimal("-80"),
        unrealized_pnl_usd=Decimal("0"),
        buy_notional_usd=Decimal("25"),
        fee_bps=10,
    )
    assert room is False and reason == "exceeds_loss_budget"

    position, reason = gate.approve_buy(
        exposure_usd=Decimal("80"),
        realized_pnl_usd=Decimal("0"),
        unrealized_pnl_usd=Decimal("0"),
        buy_notional_usd=Decimal("20"),
        fee_bps=10,
    )
    assert position is False and reason == "max_position"

    allowed, reason = gate.approve_buy(
        exposure_usd=Decimal("70"),
        realized_pnl_usd=Decimal("0"),
        unrealized_pnl_usd=Decimal("0"),
        buy_notional_usd=Decimal("25"),
        fee_bps=10,
    )
    assert allowed is True and reason == "ok"


def test_profits_do_not_raise_the_loss_ceiling():
    gate = PaperRiskGate(Decimal("100000"), Decimal("100"))
    allowed, reason = gate.approve_buy(
        exposure_usd=Decimal("0"),
        realized_pnl_usd=Decimal("500"),
        unrealized_pnl_usd=Decimal("500"),
        buy_notional_usd=Decimal("100"),
        fee_bps=10,
    )
    assert allowed is False
    assert reason == "exceeds_loss_budget"


def test_shared_risk_manager_can_veto(monkeypatch):
    gate = PaperRiskGate(Decimal("100"), Decimal("100"))
    monkeypatch.setattr(gate.book, "can_open", lambda *args, **kwargs: (False, "daily_loss_limit_reached"))
    allowed, reason = gate.approve_buy(
        exposure_usd=Decimal("0"),
        realized_pnl_usd=Decimal("0"),
        unrealized_pnl_usd=Decimal("0"),
        buy_notional_usd=Decimal("25"),
        fee_bps=10,
    )
    assert allowed is False and reason == "max_loss_limit"

    gate = PaperRiskGate(Decimal("100"), Decimal("100"))
    monkeypatch.setattr(gate.book, "remaining_loss_room", lambda: 0.0)
    allowed, reason = gate.approve_buy(
        exposure_usd=Decimal("0"),
        realized_pnl_usd=Decimal("0"),
        unrealized_pnl_usd=Decimal("0"),
        buy_notional_usd=Decimal("25"),
        fee_bps=10,
    )
    assert allowed is False and reason == "max_loss_limit"


async def test_runner_rejects_when_the_book_is_already_at_the_loss_limit(tmp_path):
    cfg = settings(tmp_path)
    state = fresh_state(cfg, NOW).model_copy(update={"realized_pnl_usd": Decimal("-100")})
    save_state(Path(cfg.state_path), state)
    config, transport = scripted_config(tmp_path, ok_responses())
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.status == "risk_rejected"
    assert result.reason == "max_loss_limit"
    assert result.fill_written is False
    saved = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
    assert saved["fill_count"] == 0
    assert Decimal(saved["realized_pnl_usd"]) == Decimal("-100")
    assert not any(row["type"] == "bitget_paper_fill" for row in records(tmp_path))


async def test_runner_honors_the_position_cap_even_if_the_decision_says_buy(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    state = fresh_state(cfg, NOW).model_copy(
        update={
            "quantity_btc": Decimal("0.001"),
            "cost_basis_usd": Decimal("90"),
            "cash_usd": Decimal("10"),
            "fill_count": 1,
        }
    )
    save_state(Path(cfg.state_path), state)
    forced = demo_decide(fresh_state(cfg, NOW), True, Decimal("25"))

    def _force(*args, **kwargs):
        return forced

    monkeypatch.setattr("src.bitget_paper.engine.demo_decide", _force)
    config, transport = scripted_config(tmp_path, ok_responses())
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.status == "risk_rejected"
    assert result.reason == "max_position"
    assert result.fill_written is False
    saved = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
    assert Decimal(saved["quantity_btc"]) == Decimal("0.001")
    assert saved["fill_count"] == 1


# --- live isolation and strict config ---------------------------------------------


def test_example_config_keeps_the_desk_on_paper_and_the_new_book_on_simulate():
    cfg = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    assert cfg["mode"] == "paper"
    assert cfg["alpaca"]["paper"] is True
    assert cfg["bitget_paper"]["execution"] == "simulate"
    assert cfg["bitget_paper"]["symbol"] == "BTCUSDT"
    assert "api_key" not in cfg["bitget_paper"]


def test_package_source_does_not_import_live_executors():
    root = Path("src/bitget_paper")
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name is not None
                assert "desk" not in name.split(".")
                assert "stock_executor" not in name
                assert "crypto_executor" not in name
                assert "alpaca" not in name
                assert "solders" not in name
    assert "bitget" not in Path("src/desk.py").read_text(encoding="utf-8")


def test_live_and_secret_configs_stop_before_any_transport(tmp_path):
    with pytest.raises(Exception) as exc:
        settings_from_section({"execution": "live"})
    assert exc.value.reason.startswith("invalid_bitget_paper")
    assert "live" not in exc.value.reason

    with pytest.raises(Exception) as exc:
        settings_from_section({"api_key": CANARIES[0], "api_secret": CANARIES[0]})
    assert exc.value.reason.startswith("forbidden_key:")
    assert CANARIES[0] not in exc.value.reason

    with pytest.raises(Exception) as exc:
        settings_from_section({"tickers_path": "/api/v3/trade/place-order"})
    assert "place-order" not in exc.value.reason

    with pytest.raises(Exception) as missing:
        settings_from_root({"mode": "live"})
    assert missing.value.reason == "bitget_paper_missing"

    with pytest.raises(Exception) as disabled:
        settings_from_section({"enabled": False})
    assert disabled.value.reason == "bitget_paper_disabled"


async def test_parent_live_mode_and_broker_secrets_never_enter_the_paper_book(tmp_path, monkeypatch):
    import src.crypto.crypto_executor as crypto_executor
    import src.desk as desk
    import src.stocks.stock_executor as stock_executor

    def boom(self, *args, **kwargs):
        raise AssertionError("exchange executor constructed")

    monkeypatch.setattr(crypto_executor.CryptoExecutor, "__init__", boom)
    monkeypatch.setattr(stock_executor.StockExecutor, "__init__", boom)
    monkeypatch.setattr(desk.TradingDesk, "__init__", boom)
    monkeypatch.setenv("BITGET_API_KEY", CANARIES[3])
    monkeypatch.setenv("BITGET_API_SECRET", CANARIES[0])
    monkeypatch.setenv("BITGET_PASSPHRASE", CANARIES[1])

    config, transport = scripted_config(tmp_path, ok_responses())
    config["mode"] = "live"
    config["alpaca"]["api_secret"] = CANARIES[0]
    config["solana"]["wallet_key"] = CANARIES[1]
    config["grok"]["api_key"] = CANARIES[2]
    result = await run_cycle(config, transport=transport, now=NOW)
    assert result.fill_written is True
    assert result.summary["execution"] == "simulate"
    assert result.summary["desk_mode_ignored"] == "live"
    blob = (tmp_path / "paper.jsonl").read_text(encoding="utf-8")
    blob += (tmp_path / "state.json").read_text(encoding="utf-8")
    blob += json.dumps(result.summary)
    for canary in CANARIES:
        assert canary not in blob
    for call in transport.calls:
        assert CANARIES[3] not in json.dumps(call)


def test_cash_that_cannot_fund_the_demo_buy_is_a_config_error(tmp_path):
    with pytest.raises(Exception) as exc:
        settings(tmp_path, demo_cash_usd="25.00")
    assert exc.value.reason.startswith("invalid_bitget_paper")


def test_corrupt_state_is_left_alone_and_does_not_call_the_network(tmp_path):
    cfg = paper_config(tmp_path)
    path = tmp_path / "state.json"
    path.write_text("{", encoding="utf-8")
    called = {"hit": False}

    async def transport(*args, **kwargs):
        called["hit"] = True
        raise AssertionError("network")

    with pytest.raises(Exception) as exc:
        await_cycle_raising(cfg, transport)
    assert exc.value.reason == "state_invalid"
    assert called["hit"] is False
    assert path.read_text(encoding="utf-8") == "{"
    assert not (tmp_path / "paper.jsonl").exists()


def test_state_rejects_unknown_and_float_fields(tmp_path):
    cfg = settings(tmp_path)
    state = fresh_state(cfg, NOW)
    payload = json.loads(state.model_dump_json())
    payload["api_key"] = CANARIES[0]
    path = Path(cfg.state_path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception) as exc:
        await_cycle_raising(paper_config(tmp_path), Script([]))
    assert exc.value.reason == "state_invalid"
    assert CANARIES[0] not in exc.value.reason
    assert CANARIES[0] in path.read_text(encoding="utf-8")


def test_cli_rejects_live_execution_and_requires_a_mode(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("bitget_paper:\n  execution: live\n", encoding="utf-8")
    assert main(["--config", str(path), "--once"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["status"] == "config_rejected"
    assert err["execution"] == "not_started"
    assert "live" not in err["reason"]

    with pytest.raises(SystemExit) as exc:
        main(["--config", str(path)])
    assert exc.value.code == 2


def test_cli_smoke_does_not_create_a_position(tmp_path, capsys):
    config = paper_config(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    script = Script(ok_responses(epoch_ms(datetime.now(timezone.utc))))
    assert main(["--config", str(path), "--smoke"], transport=script) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "smoke_ok"
    assert summary["authenticated"] is False
    assert summary["fill_written"] is False
    assert summary["observation_not_a_signal"] is True
    assert not (tmp_path / "state.json").exists()


def test_cli_once_reports_a_local_fill(tmp_path, capsys):
    config = paper_config(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert main(["--config", str(path), "--once"], transport=Script(ok_responses(epoch_ms(datetime.now(timezone.utc))))) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "filled"
    assert summary["exchange_order"] is False
    assert (tmp_path / "state.json").exists()


def test_replay_does_not_treat_a_paper_fill_as_a_desk_buy():
    import importlib.util

    spec = importlib.util.spec_from_file_location("desk_replay", Path("scripts/replay.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    summary = module.summarize(
        [
            {
                "ts": "2026-11-19T00:00:00+00:00",
                "type": "bitget_paper_fill",
                "amount": 25,
                "pnl": 10,
                "market": "crypto",
            }
        ],
        None,
        None,
    )
    assert summary["buys"] == []
    assert summary["total_pnl"] == 0


def _await(config, transport):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run_cycle(config, transport=transport, now=NOW))
    finally:
        loop.close()


def await_cycle_raising(config, transport):
    return _await(config, transport)


def test_unknown_exchange_fields_are_ignored_sync(tmp_path):
    instruments, tickers = fresh_payloads()
    inst = json.loads(instruments)
    inst["data"][0]["isRwa"] = "NO"
    inst["data"][0]["note"] = "ignore me"
    config = paper_config(tmp_path)
    result = _await(config, Script([(200, dump(inst)), (200, tickers)]))
    assert result.fill_written is True
    assert "ignore me" not in (tmp_path / "paper.jsonl").read_text(encoding="utf-8")


# --- optional public smoke --------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("BITGET_PAPER_NETWORK") != "1",
    reason="set BITGET_PAPER_NETWORK=1 to call the public Bitget API",
)
async def test_public_smoke_btcusdt_needs_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("BITGET_API_KEY", CANARIES[3])
    result = await run_cycle(paper_config(tmp_path), smoke=True)
    assert result.exit_code == 0
    assert result.summary["authenticated"] is False
    assert result.summary["symbol"] == "BTCUSDT"
    assert result.summary["observation_not_a_signal"] is True
    assert result.fill_written is False
    assert not (tmp_path / "state.json").exists()
    assert CANARIES[3] not in (tmp_path / "paper.jsonl").read_text(encoding="utf-8")


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("BITGET_PAPER_NETWORK") != "1",
    reason="set BITGET_PAPER_NETWORK=1 to call the public Bitget API",
)
async def test_public_once_simulates_a_local_fill_from_the_live_quote(tmp_path):
    result = await run_cycle(paper_config(tmp_path))
    assert result.exit_code == 0
    assert result.fill_written is True
    assert result.summary["exchange_order"] is False
    assert result.summary["authenticated"] is False
    assert Decimal(result.summary["unrealized_usd"]) < 0
    text = (tmp_path / "paper.jsonl").read_text(encoding="utf-8")
    assert "LOCAL_SIMULATED_FILL" in text
    assert "ACCESS-KEY" not in text
