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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from .config import ALLOWED_PATHS, CANDLES_PATH, PUBLIC_BASE_URL, TIME_PATH, BitgetPaperSettings
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


@dataclass(frozen=True)
class TickerSnap:
    """One accepted public ticker. Prices are already decimals; time is the server message ts."""

    bid: Decimal
    ask: Decimal
    last: Decimal
    bid_size: Decimal
    ask_size: Decimal
    server_ts_ms: int
    received_at_ms: int
    received_mono: float = 0.0


@dataclass(frozen=True)
class ClockSample:
    """REST serverTime compared with the local clock around that call.

    offset_ms is serverTime minus the local midpoint. It is not one-way delay.
    uncertainty_ms is half the round trip. A negative offset is kept.
    """

    status: str
    offset_ms: int | None
    rtt_ms: int | None
    uncertainty_ms: int | None
    server_time_ms: int | None
    sampled_at_ms: int | None


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
            source="bitget_public_rest_v3",
        )
    except QuoteRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - model backstop, no payload in the reason
        raise QuoteRejected("quote_invalid", {"error_type": type(exc).__name__}) from exc
    return quote


def parse_server_time(status: int, text: str) -> int:
    """``GET /api/v3/market/time`` body field ``data.serverTime``, a millisecond string."""
    if status != 200:
        raise QuoteRejected("http_status", {"status": status})
    if len(text) > _MAX_BODY:
        raise QuoteRejected("oversized_body")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise QuoteRejected("malformed_body") from None
    if not isinstance(body, dict) or body.get("code") != "00000":
        raise QuoteRejected("api_code")
    data = body.get("data")
    if not isinstance(data, dict):
        raise QuoteRejected("malformed_body")
    return _epoch_ms(data.get("serverTime"))


def clock_sample_from_rtt(before_ms: int, after_ms: int, server_ms: int) -> ClockSample:
    rtt = after_ms - before_ms
    if rtt < 0:
        return ClockSample("failed", None, None, None, server_ms, after_ms)
    midpoint = before_ms + rtt // 2
    return ClockSample(
        status="ok",
        offset_ms=server_ms - midpoint,
        rtt_ms=rtt,
        uncertainty_ms=rtt // 2,
        server_time_ms=server_ms,
        sampled_at_ms=after_ms,
    )


def judge_clock(sample: ClockSample, settings: BitgetPaperSettings) -> str:
    """Name the sample. Offset is not one-way delay.

    ``shifted`` means the absolute offset is past ``clock_skew_seconds``.
    A long round trip does not widen that limit or hide the offset.
    """
    if sample.offset_ms is None or sample.uncertainty_ms is None or sample.rtt_ms is None:
        return "failed"
    limit_ms = settings.clock_skew_seconds * 1000
    if abs(sample.offset_ms) > limit_ms:
        return "shifted"
    if abs(sample.offset_ms) <= sample.uncertainty_ms:
        return "within_noise"
    return "ok"


def assemble_ws_quote(
    instrument_status: int,
    instrument_text: str,
    snap: TickerSnap,
    now: datetime,
    settings: BitgetPaperSettings,
) -> ValidatedQuote:
    """REST instrument metadata plus one WebSocket ticker snapshot.

    The ticker clock is the message ``ts``, not a REST ``requestTime``.
    Freshness uses the same age and future limits as the REST book.
    """
    now_ms = epoch_ms(now)
    max_age_ms = settings.max_quote_age_seconds * 1000
    skew_ms = settings.clock_skew_seconds * 1000
    instrument, instrument_time = _envelope(instrument_status, instrument_text)
    _check_fresh(instrument_time, now_ms, max_age_ms, skew_ms, "instrument_request_time")
    # The feed already applied the monotonic receipt-age gate. Do not age the
    # ticker again with the wall clock; a jumped clock must not ban a live snap
    # or, worse, was the old way a frozen clock looked "fresh".
    if instrument.get("symbol") != "BTCUSDT" or instrument.get("category") != "SPOT":
        raise QuoteRejected("symbol_mismatch")
    ticker = {
        "symbol": "BTCUSDT",
        "category": "SPOT",
        "bid1Price": format(snap.bid, "f"),
        "ask1Price": format(snap.ask, "f"),
        "lastPrice": format(snap.last, "f"),
        "bid1Size": format(snap.bid_size, "f"),
        "ask1Size": format(snap.ask_size, "f"),
        "ts": str(snap.server_ts_ms),
    }
    # Reuse the REST price checks. requestTime for this ticker is the message ts.
    fake_ticker = json.dumps(
        {"code": "00000", "msg": "success", "requestTime": snap.server_ts_ms, "data": [ticker]}
    )
    quote = parse_public_book(instrument_status, instrument_text, 200, fake_ticker, now, settings)
    return quote.model_copy(update={"source": "bitget_public_ws_v3", "ticker_request_time_ms": snap.server_ts_ms})


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
        params = {} if path == TIME_PATH else {"category": "SPOT", "symbol": "BTCUSDT"}
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

    async def measure_clock(self) -> ClockSample:
        before = epoch_ms(datetime.now(timezone.utc))
        status, text = await self._get(TIME_PATH)
        after = epoch_ms(datetime.now(timezone.utc))
        server_ms = parse_server_time(status, text)
        sample = clock_sample_from_rtt(before, after, server_ms)
        sample = ClockSample(
            status=judge_clock(sample, self.settings),
            offset_ms=sample.offset_ms,
            rtt_ms=sample.rtt_ms,
            uncertainty_ms=sample.uncertainty_ms,
            server_time_ms=sample.server_time_ms,
            sampled_at_ms=sample.sampled_at_ms,
        )
        return sample

    async def fetch_cycle(self, now: datetime, snap: TickerSnap | None) -> tuple[ValidatedQuote, str]:
        """Prefer a fresh WebSocket snap. REST is the labeled fallback and uses the same age limit."""
        if snap is not None:
            instrument_status, instrument_text = await self._get(self.settings.instruments_path)
            try:
                return assemble_ws_quote(instrument_status, instrument_text, snap, now, self.settings), ""
            except QuoteRejected as exc:
                if exc.reason in {"transport_error", "http_status"}:
                    raise
                quote = await self.fetch(now)
                return quote, exc.reason
        quote = await self.fetch(now)
        return quote, "no_snapshot"

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
