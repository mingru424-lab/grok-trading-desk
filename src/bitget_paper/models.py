"""Strict contracts for the local Bitget paper book.

Money that crosses a boundary is a decimal string, not a float. The decision
object is checked against the same strict-object rules the desk's agents use
(``additionalProperties: false``). A contract that does not validate is not a
permission to simulate a fill.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..base_agent import enum, schema

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

UNSIGNED_DEC = r"(0|[1-9][0-9]{0,17})(\.[0-9]{1,12})?"
SIGNED_DEC = r"-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,12})?"

#: Cash headroom so fee and price rounding cannot push a configured buy over
#: the cash that was declared sufficient.
DEMO_CASH_BUFFER = Decimal("0.01")

DISCLAIMER = (
    "演示决策：确定性演示策略，不是 LLM 多 Agent 验证，不是收益预测，不代表任何预期回报。"
    " DETERMINISTIC DEMO: not an LLM multi-agent verification and not a return forecast."
)
ASSUMPTION_NOTE = (
    "演示假设：手续费和滑点不是 Bitget 手续费表，也不是交易所真实成交。"
    " Demonstration assumptions, not Bitget's fee schedule and not an exchange fill."
)
POLICY_ID = "deterministic_demo_flat_once_v1"
POLICY_RULE = "if_flat_and_no_prior_fill_then_buy_demo_notional_else_hold"

DECISION_SCHEMA = schema(
    {
        "schema_version": enum("bitget_paper_decision.v1"),
        "label": enum("DETERMINISTIC_DEMO"),
        "not_llm": {"type": "boolean", "const": True},
        "not_multi_agent_verification": {"type": "boolean", "const": True},
        "not_a_return_forecast": {"type": "boolean", "const": True},
        "action": enum("BUY", "HOLD", "NO_TRADE"),
        "symbol": enum("BTCUSDT"),
        "category": enum("SPOT"),
        "notional_usd": {"type": "string", "pattern": UNSIGNED_DEC},
        "policy_id": enum(POLICY_ID),
        "reason": {"type": "string", "minLength": 1, "maxLength": 200},
        "basis": schema(
            {
                "quote_accepted": {"type": "boolean"},
                "position_quantity_btc": {"type": "string", "pattern": UNSIGNED_DEC},
                "fill_count": {"type": "integer", "minimum": 0},
                "price_used_as_signal": {"type": "boolean", "const": False},
                "rule": enum(POLICY_RULE),
            }
        ),
        "disclaimer": {"type": "string", "const": DISCLAIMER},
    }
)


class BitgetPaperConfigError(Exception):
    """The paper book refuses to start. Callers must not start the desk instead."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StateError(Exception):
    """Local book is unreadable. Do not replace it and do not trade."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class QuoteRejected(Exception):
    """Public market data is missing or unusable. Not a fill."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class ContractError(Exception):
    """Our own output failed the strict contract. Not a fill."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BrokerRefusal(Exception):
    """The simulator declined. The previous book is unchanged."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def epoch_ms(now: datetime) -> int:
    """UTC milliseconds without going through a float timestamp."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("naive_clock")
    delta = now.astimezone(timezone.utc) - _EPOCH
    return delta // timedelta(milliseconds=1)


def from_epoch_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def conforms(instance: Any, spec: dict[str, Any], path: str = "$") -> None:
    """Reject anything the strict schema does not name.

    This is the desk's schema shape, checked in process so a demo decision
    cannot grow an ``expected_return`` field and still be logged.
    """
    if "const" in spec and instance != spec["const"]:
        raise ContractError(f"const:{path}")
    kind = spec.get("type")
    if kind == "object":
        if not isinstance(instance, dict):
            raise ContractError(f"type:{path}")
        props = spec.get("properties") or {}
        if spec.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(props))
            if extra:
                raise ContractError(f"extra:{path}:{','.join(extra)}")
        for key in spec.get("required") or []:
            if key not in instance:
                raise ContractError(f"missing:{path}.{key}")
        for key, value in instance.items():
            if key in props:
                conforms(value, props[key], f"{path}.{key}")
        return
    if kind == "string":
        if not isinstance(instance, str):
            raise ContractError(f"type:{path}")
        if "enum" in spec and instance not in spec["enum"]:
            raise ContractError(f"enum:{path}")
        if "minLength" in spec and len(instance) < spec["minLength"]:
            raise ContractError(f"minLength:{path}")
        if "maxLength" in spec and len(instance) > spec["maxLength"]:
            raise ContractError(f"maxLength:{path}")
        if "pattern" in spec and re.fullmatch(spec["pattern"], instance) is None:
            raise ContractError(f"pattern:{path}")
        return
    if kind == "boolean":
        if not isinstance(instance, bool):
            raise ContractError(f"type:{path}")
        return
    if kind == "integer":
        if isinstance(instance, bool) or not isinstance(instance, int):
            raise ContractError(f"type:{path}")
        if "minimum" in spec and instance < spec["minimum"]:
            raise ContractError(f"minimum:{path}")
        return
    raise ContractError(f"schema:{path}")


def money_str(value: Decimal, *, signed: bool = False) -> str:
    """Canonical decimal string. Scientific notation is a contract failure."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractError("non_finite_money")
    if not signed and value < 0:
        raise ContractError("negative_money")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        text = "0"
    pattern = SIGNED_DEC if signed else UNSIGNED_DEC
    if re.fullmatch(pattern, text) is None:
        raise ContractError("money_format")
    return text


def _finite_decimal(value: Any, *, signed: bool) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("decimal_must_be_string_or_int")
    if isinstance(value, int):
        decimal = Decimal(value)
    elif isinstance(value, Decimal):
        decimal = value
    elif isinstance(value, str):
        pattern = SIGNED_DEC if signed else UNSIGNED_DEC
        if re.fullmatch(pattern, value) is None:
            raise ValueError("bad_decimal")
        decimal = Decimal(value)
    else:
        raise ValueError("bad_decimal")
    if not decimal.is_finite():
        raise ValueError("bad_decimal")
    if not signed and decimal < 0:
        raise ValueError("negative_decimal")
    return decimal


class DecisionBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_accepted: bool
    position_quantity_btc: str
    fill_count: int = Field(ge=0)
    price_used_as_signal: Literal[False]
    rule: Literal[POLICY_RULE]

    @field_validator("position_quantity_btc")
    @classmethod
    def _qty(cls, value: str) -> str:
        _finite_decimal(value, signed=False)
        return value


class DemoDecision(BaseModel):
    """One deterministic demo verdict. Price is not an input to the policy."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bitget_paper_decision.v1"]
    label: Literal["DETERMINISTIC_DEMO"]
    not_llm: Literal[True]
    not_multi_agent_verification: Literal[True]
    not_a_return_forecast: Literal[True]
    action: Literal["BUY", "HOLD", "NO_TRADE"]
    symbol: Literal["BTCUSDT"]
    category: Literal["SPOT"]
    notional_usd: str
    policy_id: Literal[POLICY_ID]
    reason: str = Field(min_length=1, max_length=200)
    basis: DecisionBasis
    disclaimer: Literal[DISCLAIMER]

    @field_validator("notional_usd")
    @classmethod
    def _notional(cls, value: str) -> str:
        _finite_decimal(value, signed=False)
        return value

    @model_validator(mode="after")
    def _action_matches_notional(self) -> "DemoDecision":
        amount = Decimal(self.notional_usd)
        if self.action == "BUY" and amount <= 0:
            raise ValueError("buy_needs_notional")
        if self.action != "BUY" and amount != 0:
            raise ValueError("non_buy_must_be_zero_notional")
        return self

    def checked(self) -> "DemoDecision":
        conforms(self.model_dump(mode="json"), DECISION_SCHEMA)
        return self


class ValidatedQuote(BaseModel):
    """A BTCUSDT spot book that already survived the public-data checks."""

    model_config = ConfigDict(extra="forbid")

    symbol: Literal["BTCUSDT"]
    category: Literal["SPOT"]
    bid: Decimal
    ask: Decimal
    last: Decimal
    bid_size: Decimal
    ask_size: Decimal
    ticker_ts_ms: int
    ticker_request_time_ms: int
    instrument_request_time_ms: int
    price_precision: int = Field(ge=0, le=18)
    quantity_precision: int = Field(ge=0, le=18)
    quote_precision: int = Field(ge=0, le=18)
    min_order_amount: Decimal
    max_market_order_amount: Decimal | None = None
    min_order_qty: Decimal | None = None

    @model_validator(mode="after")
    def _book(self) -> "ValidatedQuote":
        for name in ("bid", "ask", "last", "bid_size", "ask_size", "min_order_amount"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(name)
        if self.ask < self.bid:
            raise ValueError("crossed_book")
        if self.min_order_qty is not None and (
            not self.min_order_qty.is_finite() or self.min_order_qty <= 0
        ):
            raise ValueError("min_order_qty")
        if self.max_market_order_amount is not None and (
            not self.max_market_order_amount.is_finite() or self.max_market_order_amount <= 0
        ):
            raise ValueError("max_market_order_amount")
        return self


class PaperState(BaseModel):
    """The local book. A file that fails validation is left untouched."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    label: Literal["LOCAL_PAPER_DEMO"]
    symbol: Literal["BTCUSDT"]
    category: Literal["SPOT"]
    cash_usd: Decimal
    quantity_btc: Decimal
    cost_basis_usd: Decimal
    realized_pnl_usd: Decimal
    fees_paid_usd: Decimal
    fill_count: int = Field(ge=0)
    last_mark_price: Decimal | None = None
    last_unrealized_pnl_usd: Decimal = Decimal("0")
    updated_at: datetime

    @field_validator(
        "cash_usd",
        "quantity_btc",
        "cost_basis_usd",
        "fees_paid_usd",
        mode="before",
    )
    @classmethod
    def _unsigned(cls, value: Any) -> Decimal:
        return _finite_decimal(value, signed=False)

    @field_validator("realized_pnl_usd", "last_unrealized_pnl_usd", mode="before")
    @classmethod
    def _signed(cls, value: Any) -> Decimal:
        return _finite_decimal(value, signed=True)

    @field_validator("last_mark_price", mode="before")
    @classmethod
    def _mark(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        decimal = _finite_decimal(value, signed=False)
        if decimal <= 0:
            raise ValueError("mark")
        return decimal

    @model_validator(mode="after")
    def _invariants(self) -> "PaperState":
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("naive_time")
        if self.quantity_btc == 0 and self.cost_basis_usd != 0:
            raise ValueError("flat_book_must_have_zero_cost")
        if self.quantity_btc > 0 and self.cost_basis_usd <= 0:
            raise ValueError("open_book_must_have_cost")
        if not self.realized_pnl_usd.is_finite() or not self.last_unrealized_pnl_usd.is_finite():
            raise ValueError("pnl")
        return self


class Assumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fee_bps: int
    slippage_bps: int
    fee_is_exchange_schedule: Literal[False]
    slippage_is_measured_fill: Literal[False]
    note: Literal[ASSUMPTION_NOTE]


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: Literal["bitget"]
    transport: Literal["public_rest_unauthenticated"]
    authenticated: Literal[False]
    base_url: Literal["https://api.bitget.com"]
    instruments_path: Literal["/api/v3/market/instruments"]
    tickers_path: Literal["/api/v3/market/tickers"]
    query_category: Literal["SPOT"]
    query_symbol: Literal["BTCUSDT"]
    instrument_request_time_ms: int
    ticker_request_time_ms: int
    ticker_ts_ms: int


class QuoteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bid: str
    ask: str
    last: str
    bid_size: str
    ask_size: str


class FillEconomics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: str
    quantity_btc: str
    gross_usd: str
    fee_usd: str
    cash_debited_usd: str


class PositionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity_btc: str
    cost_basis_usd: str
    cash_usd: str
    avg_entry_price: str
    fees_paid_usd: str
    fill_count: int = Field(ge=0)


class PnlSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    realized_usd: str
    unrealized_usd: str
    total_usd: str
    mark_price: str
    mark_side: Literal["bid_after_adverse_slippage_and_fee"]


class PaperFillRecord(BaseModel):
    """Audit row for one local fill. It is not an exchange acknowledgement."""

    model_config = ConfigDict(extra="forbid")

    book: Literal["bitget_local_paper"]
    label: Literal["LOCAL_SIMULATED_FILL"]
    new_fill: Literal[True]
    not_exchange_order: Literal[True]
    authenticated: Literal[False]
    strategy_expects_profit: Literal[False]
    demo: Literal[True]
    symbol: Literal["BTCUSDT"]
    category: Literal["SPOT"]
    side: Literal["BUY"]
    decision: DemoDecision
    source: SourceInfo
    assumptions: Assumptions
    quote: QuoteSnapshot
    fill: FillEconomics
    position: PositionSnapshot
    pnl: PnlSnapshot
