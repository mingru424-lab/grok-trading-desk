"""Local fill simulator.

Buys are filled at the ask, worsened by the slippage assumption and rounded
up to the instrument price precision. Sells, which the default policy never
sends, are filled at the bid, worsened and rounded down. Fees are rounded up.
Unrealized PnL is then rounded toward negative infinity so the book does not
display a kinder result than the formula.

None of these numbers is an exchange fill.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from .models import BrokerRefusal, PaperState, ValidatedQuote


def _unit(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def q_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(_unit(places), rounding="ROUND_UP")


def q_down(value: Decimal, places: int) -> Decimal:
    return value.quantize(_unit(places), rounding="ROUND_DOWN")


def _rate(bps: int) -> Decimal:
    return Decimal(bps) / Decimal(10000)


def adverse_buy_price(ask: Decimal, slippage_bps: int, price_precision: int) -> Decimal:
    raw = ask * (Decimal(1) + _rate(slippage_bps))
    price = q_up(raw, price_precision)
    if price <= 0:
        raise BrokerRefusal("fill_price_invalid")
    return price


def adverse_sell_price(bid: Decimal, slippage_bps: int, price_precision: int) -> Decimal:
    raw = bid * (Decimal(1) - _rate(slippage_bps))
    price = q_down(raw, price_precision)
    if price <= 0:
        raise BrokerRefusal("mark_price_invalid")
    return price


def mark_position(
    state: PaperState,
    quote: ValidatedQuote,
    fee_bps: int,
    slippage_bps: int,
) -> tuple[Decimal, Decimal]:
    """Conservative exit mark. Flat books have zero unrealized PnL."""
    mark_price = adverse_sell_price(quote.bid, slippage_bps, quote.price_precision)
    if state.quantity_btc == 0:
        return mark_price, Decimal("0")
    gross = state.quantity_btc * mark_price
    fee = q_up(gross * _rate(fee_bps), quote.quote_precision)
    unrealized = gross - fee - state.cost_basis_usd
    unrealized = unrealized.quantize(_unit(8), rounding=ROUND_FLOOR)
    return mark_price, unrealized


def simulate_buy(
    state: PaperState,
    quote: ValidatedQuote,
    notional: Decimal,
    fee_bps: int,
    slippage_bps: int,
    now: datetime,
) -> PaperState:
    """Return a new book. The caller's book is not mutated."""
    if notional <= 0:
        raise BrokerRefusal("notional_not_positive")
    price = adverse_buy_price(quote.ask, slippage_bps, quote.price_precision)
    quantity = q_down(notional / price, quote.quantity_precision)
    if quantity <= 0:
        raise BrokerRefusal("quantity_rounds_to_zero")
    if quote.min_order_qty is not None and quantity < quote.min_order_qty:
        raise BrokerRefusal("below_min_order_qty")
    if quantity > quote.ask_size:
        raise BrokerRefusal("insufficient_top_of_book")
    gross = quantity * price
    if gross < quote.min_order_amount:
        raise BrokerRefusal("below_min_order_amount")
    if quote.max_market_order_amount is not None and gross > quote.max_market_order_amount:
        raise BrokerRefusal("above_max_market_order_amount")
    fee = q_up(gross * _rate(fee_bps), quote.quote_precision)
    debit = gross + fee
    if debit > state.cash_usd:
        raise BrokerRefusal("insufficient_demo_cash")
    return state.model_copy(
        update={
            "cash_usd": state.cash_usd - debit,
            "quantity_btc": state.quantity_btc + quantity,
            "cost_basis_usd": state.cost_basis_usd + debit,
            "fees_paid_usd": state.fees_paid_usd + fee,
            "fill_count": state.fill_count + 1,
            "updated_at": now,
        }
    )


def simulate_sell_all(
    state: PaperState,
    quote: ValidatedQuote,
    fee_bps: int,
    slippage_bps: int,
    now: datetime,
) -> PaperState:
    """Flatten the local book. The demo policy does not call this."""
    if state.quantity_btc <= 0:
        raise BrokerRefusal("no_position")
    price = adverse_sell_price(quote.bid, slippage_bps, quote.price_precision)
    if state.quantity_btc > quote.bid_size:
        raise BrokerRefusal("insufficient_top_of_book")
    gross = state.quantity_btc * price
    fee = q_up(gross * _rate(fee_bps), quote.quote_precision)
    proceeds = gross - fee
    if proceeds < 0:
        raise BrokerRefusal("proceeds_negative")
    realized = (proceeds - state.cost_basis_usd).quantize(_unit(8), rounding=ROUND_FLOOR)
    return state.model_copy(
        update={
            "cash_usd": state.cash_usd + proceeds,
            "quantity_btc": Decimal("0"),
            "cost_basis_usd": Decimal("0"),
            "realized_pnl_usd": state.realized_pnl_usd + realized,
            "fees_paid_usd": state.fees_paid_usd + fee,
            "fill_count": state.fill_count + 1,
            "updated_at": now,
        }
    )


def exposure_usd(state: PaperState, quote: ValidatedQuote) -> Decimal:
    """Cash already spent, or the ask-side mark, whichever is larger."""
    if state.quantity_btc == 0:
        return Decimal("0")
    marked = state.quantity_btc * quote.ask
    return max(state.cost_basis_usd, marked)
