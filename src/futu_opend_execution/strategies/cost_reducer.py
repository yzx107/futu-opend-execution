"""Existing-position cost reducer strategy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from futu_opend_execution.data.market import MarketState
from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerDecision,
    CostReducerEngine,
    CostReducerExecutableIntent,
    CostReducerExecutionPolicy,
    CostReducerRules,
    CostReducerState,
    build_executable_intent,
)
from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState


@dataclass(frozen=True, slots=True)
class CostReducerStrategy:
    """A strategy that only optimizes an existing trading bucket."""

    rules: CostReducerRules
    policy: CostReducerExecutionPolicy = CostReducerExecutionPolicy()

    def evaluate(
        self,
        *,
        market: MarketState,
        inventory: InventoryState,
        state: CostReducerState,
    ) -> CostReducerExecutableIntent:
        adaptive = _to_adaptive_state(market)
        if getattr(market, "stale", False):
            return build_executable_intent(
                decision=CostReducerDecision(CostReducerAction.BLOCK, reason="market data stale"),
                market=adaptive,
                inventory=inventory,
                rules=self.rules,
                policy=self.policy,
                best_bid=market.best_bid,
                best_ask=market.best_ask,
                last_sell_price=state.last_sell_price,
            )
        decision = CostReducerEngine(self.rules).evaluate(
            inventory=inventory,
            market=adaptive,
            state=state,
        )
        return build_executable_intent(
            decision=decision,
            market=adaptive,
            inventory=inventory,
            rules=self.rules,
            policy=self.policy,
            best_bid=market.best_bid,
            best_ask=market.best_ask,
            last_sell_price=state.last_sell_price,
        )


def _to_adaptive_state(market: MarketState) -> AdaptiveMarketState:
    return AdaptiveMarketState(
        opening_vwap=market.opening_vwap,
        rolling_vwap=market.rolling_vwap,
        realized_vol=market.realized_vol,
        rolling_high=market.rolling_high,
        rolling_low=market.rolling_low,
        cumulative_turnover=market.cumulative_turnover,
        volume_delta=market.volume_delta,
        turnover_delta=market.turnover_delta,
        cumulative_field_reset_detected=False,
        tick_count=market.tick_count,
        orderbook_imbalance=market.orderbook_imbalance,
        spread_bps=market.spread_bps,
        last_price=market.last_price,
    )


def default_cost_reducer_strategy() -> CostReducerStrategy:
    return CostReducerStrategy(
        rules=CostReducerRules(
            max_spread_bps=Decimal("20"),
            min_ticks_to_activate=5,
            max_round_trips=1,
        )
    )
