"""Deterministic demo policy.

The quote's price is intentionally not an argument. Nothing here is an LLM
call, a multi-agent check, or a statement about future return. An empty book
with no prior fill buys the configured demonstration notional once. Every
other accepted quote holds. An unusable quote does not trade.
"""

from __future__ import annotations

from decimal import Decimal

from .models import (
    DISCLAIMER,
    POLICY_ID,
    POLICY_RULE,
    DecisionBasis,
    DemoDecision,
    PaperState,
    money_str,
)


def demo_decide(state: PaperState, quote_accepted: bool, notional: Decimal) -> DemoDecision:
    if not quote_accepted:
        action = "NO_TRADE"
        amount = Decimal("0")
        reason = "quote_unusable"
    elif state.quantity_btc > 0 or state.fill_count > 0:
        action = "HOLD"
        amount = Decimal("0")
        reason = "demo_position_already_open_no_exit_rule"
    else:
        action = "BUY"
        amount = notional
        reason = "demo_flat_book_one_shot_pipeline_check"
    decision = DemoDecision(
        schema_version="bitget_paper_decision.v1",
        label="DETERMINISTIC_DEMO",
        not_llm=True,
        not_multi_agent_verification=True,
        not_a_return_forecast=True,
        action=action,
        symbol="BTCUSDT",
        category="SPOT",
        notional_usd=money_str(amount),
        policy_id=POLICY_ID,
        reason=reason,
        basis=DecisionBasis(
            quote_accepted=quote_accepted,
            position_quantity_btc=money_str(state.quantity_btc),
            fill_count=state.fill_count,
            price_used_as_signal=False,
            rule=POLICY_RULE,
        ),
        disclaimer=DISCLAIMER,
    )
    return decision.checked()
