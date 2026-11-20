"""Unauthenticated Bitget public market data for BTCUSDT spot.

Checked against the public documents and a live GET on 2026-11-19:

* ``GET /api/v3/market/instruments?category=SPOT&symbol=BTCUSDT``
* ``GET /api/v3/market/tickers?category=SPOT&symbol=BTCUSDT``

Both answered ``code == "00000"`` with no API key. The demo-trading REST
surface is a different thing: it requires a Demo API Key and a ``paptrading``
header, and this client does not call it. Spot instrument payloads do not
carry a fee schedule; fees in the paper book are local assumptions.

A response that is stale, crossed, the wrong symbol, or not an online crypto
spot market raises ``QuoteRejected`` and must not be turned into a fill.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from .config import ALLOWED_PATHS, CANDLES_PATH, PUBLIC_BASE_URL, BitgetPaperSettings
from .models import QuoteRejected, ValidatedQuote, epoch_ms

_API_DECIMAL = re.compile(r"(0|[1-9][0-9]{0,24})(\.[0-9]{1,18})?")
_PLACES = re.compile(r"(0|[1-9]|1[0-8])")
_MS = re.compile(r"[0-9]{13}")

PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "grok-trading-desk-bitget-public-paper/0",
}

_MAX_BODY = 1_000_000


def _api_decimal(value: Any, *, positive: bool, reason: str) -> Decimal:
    # Official prices and sizes are strings. A JSON number is a different
    # payload than the one we checked, so it is rejected rather than coerced.
    if not isinstance(value, str) or _API_DECIMAL.fullmatch(value) is None:
        raise QuoteRejected("bad_decimal", {"field_reason": reason})
    decimal = Decimal(value)
    if not decimal.is_finite() or (positive and decimal <= 0) or decimal < 0:
        raise QuoteRejected(reason)
    return decimal


def _places(value: Any) -> int:
    if not isinstance(value, str) or _PLACES.fullmatch(value) is None:
        raise QuoteRejected("bad_precision")
    return int(value)


def _epoch_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise QuoteRejected("bad_timestamp")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or _MS.fullmatch(str(value)) is None:
            raise QuoteRejected("bad_timestamp")
        return value
    if isinstance(value, str) and _MS.fullmatch(value):
        return int(value)
    raise QuoteRejected("bad_timestamp")


def _need(row: dict[str, Any], key: str) -> Any:
    if key not in row or row[key] is None:
        raise QuoteRejected("missing_field", {"field": key})
    return row[key]


def _optional_cap(value: Any) -> Decimal | None:
    """Bitget uses ``"0"`` and ``""`` for "no limit" on several spot fields."""
    if value is None or value == "":
        return None
    decimal = _api_decimal(value, positive=False, reason="bad_decimal")
    if decimal == 0:
        return None
    if decimal < 0:
        raise QuoteRejected("bad_decimal")
    return decimal


def _check_fresh(ms: int, now_ms: int, max_age_ms: int, skew_ms: int, field: str) -> None:
    if ms > now_ms + skew_ms:
        raise QuoteRejected("future_quote", {"field": field, "delta_ms": ms - now_ms})
    age = now_ms - ms
    if age > max_age_ms:
        raise QuoteRejected(
            "stale_quote", {"field": field, "age_ms": age, "max_age_ms": max_age_ms}
        )


class Candle:
    """One public kline. Fields are the exchange's list order, not a signal."""

    __slots__ = ("ts_ms", "open", "high", "low", "close", "volume")

    def __init__(self, ts_ms: int, open_: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal):
        self.ts_ms = ts_ms
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


INTERVAL_MS = {"1m": 60_000}


def _envelope_rows(status: int, text: str) -> tuple[list[Any], int]:
    if status != 200:
        raise QuoteRejected("http_status", {"status": status})
    if len(text) > _MAX_BODY:
        raise QuoteRejected("oversized_body")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise QuoteRejected("malformed_body") from None
    if not isinstance(body, dict):
        raise QuoteRejected("malformed_body")
    code = body.get("code")
    if code != "00000":
        shown = code if isinstance(code, str) else type(code).__name__
        raise QuoteRejected("api_code", {"code": str(shown)[:32]})
    rows = body.get("data")
    if not isinstance(rows, list) or not rows:
        raise QuoteRejected("empty_data")
    return rows, _epoch_ms(body.get("requestTime"))


def _envelope(status: int, text: str) -> tuple[dict[str, Any], int]:
    if status != 200:
        raise QuoteRejected("http_status", {"status": status})
    if len(text) > _MAX_BODY:
        raise QuoteRejected("oversized_body")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise QuoteRejected("malformed_body") from None
    if not isinstance(body, dict):
        raise QuoteRejected("malformed_body")
    code = body.get("code")
    if code != "00000":
        shown = code if isinstance(code, str) else type(code).__name__
        raise QuoteRejected("api_code", {"code": str(shown)[:32]})
    rows = body.get("data")
    if not isinstance(rows, list):
        raise QuoteRejected("empty_data")
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise QuoteRejected("unexpected_row_count", {"rows": len(rows)})
    return rows[0], _epoch_ms(body.get("requestTime"))


def parse_public_book(
    instrument_status: int,
    instrument_text: str,
    ticker_status: int,
    ticker_text: str,
    now: datetime,
    settings: BitgetPaperSettings,
) -> ValidatedQuote:
    """Validate both public payloads. Unknown fields are ignored, never traded on."""
    now_ms = epoch_ms(now)
    max_age_ms = settings.max_quote_age_seconds * 1000
    skew_ms = settings.clock_skew_seconds * 1000

    instrument, instrument_time = _envelope(instrument_status, instrument_text)
    ticker, ticker_time = _envelope(ticker_status, ticker_text)
    for stamp, field in (
        (instrument_time, "instrument_request_time"),
        (ticker_time, "ticker_request_time"),
        (_epoch_ms(_need(ticker, "ts")), "ticker_ts"),
    ):
        _check_fresh(stamp, now_ms, max_age_ms, skew_ms, field)

    if instrument.get("symbol") != "BTCUSDT" or ticker.get("symbol") != "BTCUSDT":
        raise QuoteRejected("symbol_mismatch")
    if instrument.get("category") != "SPOT" or ticker.get("category") != "SPOT":
        raise QuoteRejected("category_mismatch")
    if instrument.get("baseCoin") != "BTC" or instrument.get("quoteCoin") != "USDT":
        raise QuoteRejected("not_btcusdt_spot")
    # Stock tokens are identified by isReality. A missing flag is not a "no".
    if "isReality" not in instrument:
        raise QuoteRejected("missing_field", {"field": "isReality"})
    if str(instrument.get("isReality")).lower() != "no":
        raise QuoteRejected("reality_token_out_of_scope")
    if "symbolType" not in instrument or "instrumentType" not in instrument:
        raise QuoteRejected("missing_field", {"field": "symbolType"})
    if str(instrument.get("symbolType")).lower() != "crypto":
        raise QuoteRejected("not_spot_crypto")
    if str(instrument.get("instrumentType")).lower() != "crypto":
        raise QuoteRejected("not_spot_crypto")
    if instrument.get("status") != "online":
        raise QuoteRejected("instrument_not_online")
    maintain = instrument.get("maintainTime", None)
    if not isinstance(maintain, str):
        raise QuoteRejected("missing_field", {"field": "maintainTime"})
    if maintain.strip():
        raise QuoteRejected("instrument_maintenance")

    bid = _api_decimal(_need(ticker, "bid1Price"), positive=True, reason="non_positive_price")
    ask = _api_decimal(_need(ticker, "ask1Price"), positive=True, reason="non_positive_price")
    last = _api_decimal(_need(ticker, "lastPrice"), positive=True, reason="non_positive_price")
    bid_size = _api_decimal(_need(ticker, "bid1Size"), positive=True, reason="non_positive_size")
    ask_size = _api_decimal(_need(ticker, "ask1Size"), positive=True, reason="non_positive_size")
    if ask < bid:
        raise QuoteRejected("crossed_book")
    spread = (ask - bid) / bid
    if spread > settings.max_spread_ratio:
        raise QuoteRejected("spread_too_wide")
    mid = (ask + bid) / Decimal(2)
    if abs(last - mid) / mid > settings.max_last_deviation_ratio:
        raise QuoteRejected("last_outside_book")

    try:
        quote = ValidatedQuote(
            symbol="BTCUSDT",
            category="SPOT",
            bid=bid,
            ask=ask,
            last=last,
            bid_size=bid_size,
            ask_size=ask_size,
            ticker_ts_ms=_epoch_ms(ticker["ts"]),
            ticker_request_time_ms=ticker_time,
            instrument_request_time_ms=instrument_time,
            price_precision=_places(_need(instrument, "pricePrecision")),
            quantity_precision=_places(_need(instrument, "quantityPrecision")),
            quote_precision=_places(_need(instrument, "quotePrecision")),
            min_order_amount=_api_decimal(
                _need(instrument, "minOrderAmount"), positive=True, reason="bad_decimal"
            ),
            max_market_order_amount=_optional_cap(instrument.get("maxMarketOrderAmount")),
            min_order_qty=_optional_cap(instrument.get("minOrderQty")),
        )
    except QuoteRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - model backstop, no payload in the reason
        raise QuoteRejected("quote_invalid", {"error_type": type(exc).__name__}) from exc
    return quote


def parse_candles(status: int, text: str, now: datetime, settings: BitgetPaperSettings) -> list[Candle]:
    """Closed 1m bars only. A forming bar is dropped. Gaps and stale bars do not trade."""
    rows, request_time = _envelope_rows(status, text)
    now_ms = epoch_ms(now)
    interval = INTERVAL_MS[settings.candle_interval]
    _check_fresh(
        request_time,
        now_ms,
        settings.max_quote_age_seconds * 1000,
        settings.clock_skew_seconds * 1000,
        "candle_request_time",
    )
    parsed: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise QuoteRejected("malformed_body")
        ts = _epoch_ms(row[0])
        open_ = _api_decimal(row[1], positive=True, reason="non_positive_price")
        high = _api_decimal(row[2], positive=True, reason="non_positive_price")
        low = _api_decimal(row[3], positive=True, reason="non_positive_price")
        close = _api_decimal(row[4], positive=True, reason="non_positive_price")
        volume = _api_decimal(row[5], positive=False, reason="bad_decimal")
        if high < low or high < max(open_, close) or low > min(open_, close):
            raise QuoteRejected("candle_range")
        parsed.append(Candle(ts, open_, high, low, close, volume))
    parsed.sort(key=lambda candle: candle.ts_ms)
    closed = [candle for candle in parsed if candle.ts_ms + interval <= now_ms + settings.clock_skew_seconds * 1000]
    if len(closed) < settings.slow_period:
        raise QuoteRejected("insufficient_history", {"rows": len(closed), "need": settings.slow_period})
    for older, newer in zip(closed, closed[1:]):
        if newer.ts_ms - older.ts_ms != interval:
            raise QuoteRejected("candle_gap")
    latest = closed[-1]
    age = now_ms - latest.ts_ms
    if age > 2 * interval + settings.clock_skew_seconds * 1000:
        raise QuoteRejected("stale_candles", {"age_ms": age})
    if latest.ts_ms > now_ms + settings.clock_skew_seconds * 1000:
        raise QuoteRejected("future_quote", {"field": "candle_ts"})
    return closed


class PublicMarketClient:
    """GET the two public endpoints. The header set is closed."""

    def __init__(self, settings: BitgetPaperSettings, transport=None):
        self.settings = settings
        self._transport = transport or self._httpx_transport

    async def _httpx_transport(self, method: str, url: str, params: dict[str, str], headers: dict[str, str]):
        if method != "GET":
            raise QuoteRejected("transport_error", {"error_type": "MethodNotAllowed"})
        timeout = httpx.Timeout(self.settings.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, params=params, headers=headers)
            return response.status_code, response.text

    async def _get(self, path: str, extra: dict[str, str] | None = None) -> tuple[int, str]:
        if path not in ALLOWED_PATHS or self.settings.public_base_url != PUBLIC_BASE_URL:
            raise QuoteRejected("path_not_allowed")
        headers = dict(PUBLIC_HEADERS)
        if set(headers) != {"Accept", "User-Agent"}:
            raise QuoteRejected("path_not_allowed")
        url = PUBLIC_BASE_URL + path
        params = {"category": "SPOT", "symbol": "BTCUSDT"}
        for key, value in (extra or {}).items():
            if key not in {"interval", "limit"}:
                raise QuoteRejected("path_not_allowed")
            params[key] = value
        try:
            status, text = await self._transport("GET", url, params, headers)
        except QuoteRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - disconnect, timeout, DNS
            raise QuoteRejected("transport_error", {"error_type": type(exc).__name__}) from exc
        if not isinstance(status, int) or isinstance(status, bool) or not isinstance(text, str):
            raise QuoteRejected("transport_error", {"error_type": "BadTransportResult"})
        return status, text

    async def fetch(self, now: datetime) -> ValidatedQuote:
        instrument_status, instrument_text = await self._get(self.settings.instruments_path)
        ticker_status, ticker_text = await self._get(self.settings.tickers_path)
        return parse_public_book(
            instrument_status,
            instrument_text,
            ticker_status,
            ticker_text,
            now,
            self.settings,
        )

    async def fetch_candles(self, now: datetime) -> list[Candle]:
        if self.settings.candles_path != CANDLES_PATH:
            raise QuoteRejected("path_not_allowed")
        limit = str(self.settings.slow_period + 2)
        status, text = await self._get(
            self.settings.candles_path,
            {"interval": self.settings.candle_interval, "limit": limit},
        )
        return parse_candles(status, text, now, self.settings)
