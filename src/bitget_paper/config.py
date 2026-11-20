"""Load the paper-book section and refuse anything that is not local simulation.

The parent desk config may contain Alpaca keys or ``mode: live``. Those keys
are not read. A bad ``bitget_paper`` block raises ``BitgetPaperConfigError``
and has no second path into ``TradingDesk``.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    DEMO_CASH_BUFFER,
    LEGACY_POLICY_ID,
    SMA_POLICY_ID,
    BitgetPaperConfigError,
)

PUBLIC_BASE_URL = "https://api.bitget.com"
INSTRUMENTS_PATH = "/api/v3/market/instruments"
TICKERS_PATH = "/api/v3/market/tickers"
CANDLES_PATH = "/api/v3/market/candles"
ALLOWED_PATHS = frozenset({INSTRUMENTS_PATH, TICKERS_PATH, CANDLES_PATH})

# Key names only. Values are never copied into the error text.
_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "apikey",
    "api_secret",
    "apisecret",
    "secret",
    "passphrase",
    "access_key",
    "access_sign",
    "access_passphrase",
    "access_timestamp",
    "private_key",
    "wallet_key",
    "paptrading",
    "authorization",
)


def _key_is_forbidden(key: str) -> bool:
    norm = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if norm in {"token"}:
        return True
    return any(part in norm for part in _FORBIDDEN_KEY_PARTS)


def reject_forbidden_keys(value: Any, path: str = "bitget_paper") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _key_is_forbidden(str(key)):
                raise BitgetPaperConfigError(f"forbidden_key:{path}.{key}")
            reject_forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_forbidden_keys(item, f"{path}[{index}]")


def parse_config_decimal(value: Any) -> Decimal:
    """YAML numbers arrive as int or float. Floats are accepted only via ``str``."""
    if isinstance(value, bool) or value is None:
        raise ValueError("bad_decimal")
    if isinstance(value, Decimal):
        decimal = value
    elif isinstance(value, int):
        decimal = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("bad_decimal")
        decimal = Decimal(str(value))
    elif isinstance(value, str):
        if re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value) is None:
            raise ValueError("bad_decimal")
        decimal = Decimal(value)
    else:
        raise ValueError("bad_decimal")
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("bad_decimal")
    return decimal


def _usd(value: Any) -> Decimal:
    decimal = parse_config_decimal(value)
    if decimal != decimal.quantize(Decimal("0.01")):
        raise ValueError("use_cent_precision")
    return decimal


def _ratio(value: Any) -> Decimal:
    return parse_config_decimal(value)


class BitgetPaperSettings(BaseModel):
    """Demonstration defaults. They are not a forecast and not a fee schedule."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    execution: Literal["simulate"] = "simulate"
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    category: Literal["SPOT"] = "SPOT"
    public_base_url: Literal[PUBLIC_BASE_URL] = PUBLIC_BASE_URL
    instruments_path: Literal[INSTRUMENTS_PATH] = INSTRUMENTS_PATH
    tickers_path: Literal[TICKERS_PATH] = TICKERS_PATH
    candles_path: Literal[CANDLES_PATH] = CANDLES_PATH
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_quote_age_seconds: int = Field(default=15, ge=1, le=30)
    clock_skew_seconds: int = Field(default=2, ge=0, le=5)
    max_spread_ratio: Decimal = Decimal("0.01")
    max_last_deviation_ratio: Decimal = Decimal("0.005")
    strategy: Literal[SMA_POLICY_ID, LEGACY_POLICY_ID] = SMA_POLICY_ID
    fast_period: int = Field(default=5, ge=2, le=50)
    slow_period: int = Field(default=20, ge=3, le=100)
    candle_interval: Literal["1m"] = "1m"
    demo_notional_usd: Decimal = Decimal("25")
    demo_cash_usd: Decimal = Decimal("100")
    fee_bps: int = Field(default=10, ge=0, le=100)
    slippage_bps: int = Field(default=5, ge=0, le=100)
    max_position_usd: Decimal = Decimal("100")
    max_loss_usd: Decimal = Decimal("100")
    max_drawdown_pct: Decimal = Decimal("0.25")
    poll_seconds: int = Field(default=20, ge=5, le=300)
    max_cycle_retries: int = Field(default=2, ge=0, le=3)
    retry_backoff_seconds: Decimal = Decimal("0.5")
    emergency_flatten: bool = False
    status_enabled: bool = True
    status_host: Literal["127.0.0.1"] = "127.0.0.1"
    status_port: int = Field(default=8765, ge=1024, le=65535)
    log_path: str = "logs/bitget_paper.jsonl"
    state_path: str = "logs/bitget_paper_state.json"
    equity_path: str = "logs/bitget_paper_equity.jsonl"
    control_path: str = "logs/bitget_paper_control.json"
    status_path: str = "logs/bitget_paper_status.json"
    echo_log: bool = False

    @field_validator(
        "demo_notional_usd",
        "demo_cash_usd",
        "max_position_usd",
        "max_loss_usd",
        mode="before",
    )
    @classmethod
    def _usd_fields(cls, value: Any) -> Decimal:
        return _usd(value)

    @field_validator("max_spread_ratio", "max_last_deviation_ratio", "max_drawdown_pct", mode="before")
    @classmethod
    def _ratio_fields(cls, value: Any) -> Decimal:
        return _ratio(value)

    @field_validator("retry_backoff_seconds", mode="before")
    @classmethod
    def _backoff(cls, value: Any) -> Decimal:
        return _ratio(value)

    @field_validator("log_path", "state_path", "equity_path", "control_path", "status_path")
    @classmethod
    def _paths(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("bad_path")
        if value.startswith(("http://", "https://")) or "\x00" in value:
            raise ValueError("bad_path")
        return value

    @model_validator(mode="after")
    def _demo_bounds(self) -> "BitgetPaperSettings":
        if self.max_spread_ratio <= 0 or self.max_spread_ratio > Decimal("0.02"):
            raise ValueError("max_spread_ratio")
        if self.max_last_deviation_ratio <= 0 or self.max_last_deviation_ratio > Decimal("0.02"):
            raise ValueError("max_last_deviation_ratio")
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct > Decimal("1"):
            raise ValueError("max_drawdown_pct")
        if self.retry_backoff_seconds <= 0 or self.retry_backoff_seconds > Decimal("5"):
            raise ValueError("retry_backoff_seconds")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period_must_be_shorter_than_slow")
        if self.demo_notional_usd <= 0 or self.max_position_usd <= 0 or self.max_loss_usd <= 0:
            raise ValueError("limits_must_be_positive")
        if self.demo_cash_usd <= 0:
            raise ValueError("demo_cash_must_be_positive")
        paths = {
            self.log_path,
            self.state_path,
            self.equity_path,
            self.control_path,
            self.status_path,
        }
        if len(paths) != 5:
            raise ValueError("book_paths_must_differ")
        fee = Decimal(self.fee_bps) / Decimal(10000)
        # Worst-case cash and exposure of the one demo buy, including the
        # rounding buffer. A config that cannot pass its own risk gate does
        # not start.
        projected = self.demo_notional_usd * (Decimal(1) + fee) + DEMO_CASH_BUFFER
        if projected > self.demo_cash_usd:
            raise ValueError("demo_cash_cannot_fund_notional_plus_assumed_fee")
        if projected > self.max_position_usd:
            raise ValueError("demo_notional_exceeds_max_position")
        if projected > self.max_loss_usd:
            raise ValueError("demo_notional_exceeds_max_loss")
        if self.execution != "simulate":
            raise ValueError("execution_not_simulate")
        return self


def _validation_reason(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return "invalid_bitget_paper"
    parts: list[str] = []
    for err in errors():
        loc = ".".join(str(item) for item in err.get("loc", ())) or "bitget_paper"
        parts.append(f"{loc}:{err.get('type', 'invalid')}")
    return "invalid_bitget_paper:" + ",".join(parts) if parts else "invalid_bitget_paper"


def settings_from_section(section: Any) -> BitgetPaperSettings:
    if not isinstance(section, dict):
        raise BitgetPaperConfigError("bitget_paper_not_a_mapping")
    reject_forbidden_keys(section)
    try:
        settings = BitgetPaperSettings.model_validate(section)
    except Exception as exc:  # noqa: BLE001 - rewritten without input values
        raise BitgetPaperConfigError(_validation_reason(exc)) from exc
    if not settings.enabled:
        raise BitgetPaperConfigError("bitget_paper_disabled")
    if settings.execution != "simulate":
        raise BitgetPaperConfigError("execution_not_simulate")
    return settings


def load_raw_config(path: str | Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BitgetPaperConfigError("config_unreadable") from exc
    if not isinstance(raw, dict):
        raise BitgetPaperConfigError("config_not_a_mapping")
    return raw


def settings_from_root(root: dict[str, Any]) -> BitgetPaperSettings:
    """Read only ``bitget_paper``. Missing means stop, not "run the desk"."""
    if not isinstance(root, dict):
        raise BitgetPaperConfigError("config_not_a_mapping")
    if "bitget_paper" not in root:
        raise BitgetPaperConfigError("bitget_paper_missing")
    return settings_from_section(root["bitget_paper"])


def load_settings(path: str | Path) -> BitgetPaperSettings:
    return settings_from_root(load_raw_config(path))
