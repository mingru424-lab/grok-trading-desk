"""Position and loss gate for the one-symbol paper book.

The shared ``RiskManager`` still owns the daily-loss predicate and the rule
that a profit does not increase the loss allowance. This gate adds the
absolute notional cap the desk's percentage sizer does not express for a
single demonstration symbol, and it counts unrealized loss.

A breach rejects the order. The size is never silently cut down to fit.
The loss counter is not reset at midnight: a new calendar day is not
permission to buy again.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import Allocation, Market
from ..shared.risk import RiskManager
from .models import DEMO_CASH_BUFFER, BitgetPaperConfigError


def _rate(fee_bps: int) -> Decimal:
    return Decimal(fee_bps) / Decimal(10000)


class PaperRiskGate:
    def __init__(self, max_position_usd: Decimal, max_loss_usd: Decimal):
        if max_position_usd <= 0 or max_loss_usd <= 0:
            raise BitgetPaperConfigError("risk_limits_not_positive")
        self.max_position_usd = max_position_usd
        self.max_loss_usd = max_loss_usd
        self.book = RiskManager(
            {
                "risk": {
                    "total_budget_usd": float(max_position_usd),
                    "daily_loss_limit_usd": float(max_loss_usd),
                    "max_open_total": 1,
                    "max_open_crypto": 1,
                    "max_open_stocks": 0,
                    "max_per_sector": 1,
                    "crypto_max_pct": 1.0,
                    "stock_max_pct": 0.0,
                    "max_position_pct_of_market": 1.0,
                    "max_position_pct_of_remaining_loss": 1.0,
                }
            }
        )
        self.book.set_allocation(Allocation(crypto_pct=1.0, stocks_pct=0.0))
        budget = Decimal(str(round(self.book.market_budget(Market.CRYPTO), 2)))
        if budget != max_position_usd.quantize(Decimal("0.01")):
            raise BitgetPaperConfigError("risk_budget_mismatch")

    def approve_buy(
        self,
        *,
        exposure_usd: Decimal,
        realized_pnl_usd: Decimal,
        unrealized_pnl_usd: Decimal,
        buy_notional_usd: Decimal,
        fee_bps: int,
    ) -> tuple[bool, str]:
        if buy_notional_usd <= 0 or not buy_notional_usd.is_finite():
            return False, "notional_not_positive"
        if exposure_usd < 0 or not exposure_usd.is_finite():
            return False, "bad_exposure"

        # Shared book: realized loss, and profits do not refill the allowance.
        self.book.realized_pnl_today = float(realized_pnl_usd)
        allowed, reason = self.book.can_open(Market.CRYPTO, [])
        if not allowed:
            if reason == "daily_loss_limit_reached":
                return False, "max_loss_limit"
            return False, "risk_rejected"
        if self.book.daily_loss_breached() or self.book.remaining_loss_room() <= 0:
            return False, "max_loss_limit"

        total = realized_pnl_usd + unrealized_pnl_usd
        if total <= -self.max_loss_usd:
            return False, "max_loss_limit"
        loss_used = -min(Decimal("0"), total)
        room = self.max_loss_usd - loss_used
        projected = buy_notional_usd * (Decimal(1) + _rate(fee_bps)) + DEMO_CASH_BUFFER
        if projected > room:
            return False, "exceeds_loss_budget"
        if exposure_usd + projected > self.max_position_usd:
            return False, "max_position"
        return True, "ok"
