"""Local paper strategies.

``baseline_sma_cross_v1`` is the product default. It compares a short average
of closed 1m candle closes with a longer one. Fast above slow and a flat book
buys the configured notional. Fast below slow and a long book sells the whole
position. Anything else holds. The averages are a baseline rule, not evidence
of an edge.

``deterministic_demo_flat_once_v1`` remains selectable. It ignores price and
buys once. It is not the default.
"""

from __future__ import annotations

from decimal import Decimal

from .decision import demo_decide
from .market import Candle
from .models import (
    DISCLAIMER,
    LEGACY_POLICY_ID,
    LEGACY_RULE,
    SMA_POLICY_ID,
    SMA_RULE,
    DecisionBasis,
    DemoDecision,
    PaperState,
    money_str,
)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def sma_decide(
    state: PaperState,
    candles: list[Candle],
    notional: Decimal,
    fast_period: int,
    slow_period: int,
    correlation_id: str,
) -> DemoDecision:
    closes = [candle.close for candle in candles[-slow_period:]]
    fast = _mean(closes[-fast_period:])
    slow = _mean(closes)
    bar_ts = candles[-1].ts_ms
    flat = state.quantity_btc == 0
    if fast > slow and flat:
        action = "BUY"
        amount = notional
        reason = "baseline_fast_sma_above_slow"
    elif fast < slow and not flat:
        action = "SELL"
        amount = state.quantity_btc * candles[-1].close
        reason = "baseline_fast_sma_below_slow_close"
    elif fast == slow:
        action = "HOLD"
        amount = Decimal("0")
        reason = "baseline_sma_tied"
    elif not flat:
        action = "HOLD"
        amount = Decimal("0")
        reason = "baseline_keep_long"
    else:
        action = "HOLD"
        amount = Decimal("0")
        reason = "baseline_stay_flat"
    decision = DemoDecision(
        schema_version="bitget_paper_decision.v2",
        label="PAPER_RULE",
        not_llm=True,
        not_multi_agent_verification=True,
        not_a_return_forecast=True,
        action=action,
        symbol="BTCUSDT",
        category="SPOT",
        notional_usd=money_str(amount),
        policy_id=SMA_POLICY_ID,
        correlation_id=correlation_id,
        reason=reason,
        basis=DecisionBasis(
            quote_accepted=True,
            position_quantity_btc=money_str(state.quantity_btc),
            fill_count=state.fill_count,
            price_used_as_signal=True,
            rule=SMA_RULE,
            fast_sma=money_str(fast),
            slow_sma=money_str(slow),
            bar_ts_ms=bar_ts,
        ),
        disclaimer=DISCLAIMER,
    )
    return decision.checked()


def no_trade(
    state: PaperState,
    strategy: str,
    correlation_id: str,
    reason: str,
) -> DemoDecision:
    policy = strategy if strategy in {LEGACY_POLICY_ID, SMA_POLICY_ID} else SMA_POLICY_ID
    rule = LEGACY_RULE if policy == LEGACY_POLICY_ID else SMA_RULE
    decision = DemoDecision(
        schema_version="bitget_paper_decision.v2",
        label="PAPER_RULE",
        not_llm=True,
        not_multi_agent_verification=True,
        not_a_return_forecast=True,
        action="NO_TRADE",
        symbol="BTCUSDT",
        category="SPOT",
        notional_usd="0",
        policy_id=policy,
        correlation_id=correlation_id,
        reason=reason,
        basis=DecisionBasis(
            quote_accepted=False,
            position_quantity_btc=money_str(state.quantity_btc),
            fill_count=state.fill_count,
            price_used_as_signal=False,
            rule=rule,
            fast_sma="0",
            slow_sma="0",
            bar_ts_ms=0,
        ),
        disclaimer=DISCLAIMER,
    )
    return decision.checked()


def decide_cycle(
    state: PaperState,
    *,
    strategy: str,
    quote_accepted: bool,
    notional: Decimal,
    candles: list[Candle] | None,
    fast_period: int,
    slow_period: int,
    correlation_id: str,
) -> DemoDecision:
    if not quote_accepted:
        return no_trade(state, strategy, correlation_id, "quote_unusable")
    if strategy == LEGACY_POLICY_ID:
        return demo_decide(state, True, notional, correlation_id=correlation_id)
    if not candles:
        return no_trade(state, strategy, correlation_id, "candles_unusable")
    return sma_decide(state, candles, notional, fast_period, slow_period, correlation_id)
