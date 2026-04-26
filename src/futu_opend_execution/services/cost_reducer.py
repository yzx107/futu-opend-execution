"""Dry-run/replay-only grey-market cost reducer decision engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState


class CostReducerAction(str, Enum):
    WAIT = "WAIT"
    SELL_TRADING = "SELL_TRADING"
    REBUY_TRADING = "REBUY_TRADING"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class CostReducerRules:
    core_ratio: Decimal = Decimal("0.5")
    trading_ratio: Decimal = Decimal("0.5")
    overextension_vol_multiple: Decimal = Decimal("2.0")
    high_pullback_vol_multiple: Decimal = Decimal("0.5")
    rebuy_anchor_vol_band: Decimal = Decimal("1.0")
    max_sell_total_position_ratio: Decimal = Decimal("0.25")
    max_round_trips: int = 1
    min_turnover_to_activate: Decimal = Decimal("0")
    min_ticks_to_activate: int = 5
    max_spread_bps: Decimal = Decimal("20")
    estimated_roundtrip_cost_bps: Decimal = Decimal("10")
    safety_buffer_bps: Decimal = Decimal("5")


@dataclass(slots=True)
class CostReducerState:
    round_trips_completed: int = 0
    last_sell_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CostReducerDecision:
    action: CostReducerAction
    quantity: int = 0
    reason: str = ""


class CostReducerEngine:
    def __init__(self, rules: CostReducerRules) -> None:
        self._rules = rules

    def evaluate(
        self,
        *,
        inventory: InventoryState,
        market: AdaptiveMarketState,
        state: CostReducerState,
    ) -> CostReducerDecision:
        if state.round_trips_completed >= self._rules.max_round_trips:
            return CostReducerDecision(CostReducerAction.BLOCK, reason="max_round_trips reached")
        if market.last_price is None or market.last_price <= 0:
            return CostReducerDecision(CostReducerAction.WAIT, reason="last_price unavailable")

        activation_ok = (
            market.cumulative_turnover >= self._rules.min_turnover_to_activate
            or market.tick_count >= self._rules.min_ticks_to_activate
        )
        if not activation_ok:
            return CostReducerDecision(CostReducerAction.WAIT, reason="activation threshold not met")

        anchor = market.opening_vwap or market.rolling_vwap
        if anchor is None or market.realized_vol <= 0:
            return CostReducerDecision(CostReducerAction.WAIT, reason="anchor/vol unavailable")

        if market.spread_bps > self._rules.max_spread_bps:
            return CostReducerDecision(CostReducerAction.WAIT, reason="spread too wide")

        sell_decision = self._evaluate_sell(inventory=inventory, market=market, anchor=anchor)
        if sell_decision is not None:
            return sell_decision

        rebuy_decision = self._evaluate_rebuy(
            inventory=inventory,
            market=market,
            anchor=anchor,
            state=state,
        )
        if rebuy_decision is not None:
            return rebuy_decision

        return CostReducerDecision(CostReducerAction.WAIT, reason="conditions not met")

    def _evaluate_sell(
        self,
        *,
        inventory: InventoryState,
        market: AdaptiveMarketState,
        anchor: Decimal,
    ) -> CostReducerDecision | None:
        if inventory.trading_available_to_sell <= 0:
            return None
        if market.orderbook_imbalance > 0:
            return None
        overextended = market.last_price > (anchor + self._rules.overextension_vol_multiple * market.realized_vol)
        if not overextended:
            return None
        if market.rolling_high is None:
            return None
        pulled_back = (market.rolling_high - market.last_price) >= (
            self._rules.high_pullback_vol_multiple * market.realized_vol
        )
        if not pulled_back:
            return None

        max_sell_by_ratio = int(Decimal(inventory.current_position) * self._rules.max_sell_total_position_ratio)
        quantity = min(inventory.trading_available_to_sell, max_sell_by_ratio)
        if quantity <= 0:
            return CostReducerDecision(CostReducerAction.BLOCK, reason="sell quantity blocked by ratio")

        return CostReducerDecision(CostReducerAction.SELL_TRADING, quantity=quantity, reason="sell conditions met")

    def _evaluate_rebuy(
        self,
        *,
        inventory: InventoryState,
        market: AdaptiveMarketState,
        anchor: Decimal,
        state: CostReducerState,
    ) -> CostReducerDecision | None:
        if inventory.trading_available_to_rebuy <= 0:
            return None
        if state.last_sell_price is None or state.last_sell_price <= 0:
            return None
        if market.orderbook_imbalance <= 0:
            return None

        threshold = state.last_sell_price * (
            Decimal("1")
            - (self._rules.estimated_roundtrip_cost_bps + self._rules.safety_buffer_bps) / Decimal("10000")
        )
        if market.last_price >= threshold:
            return None

        near_anchor = abs(market.last_price - anchor) <= (
            self._rules.rebuy_anchor_vol_band * market.realized_vol
        )
        if not near_anchor:
            return None

        return CostReducerDecision(
            CostReducerAction.REBUY_TRADING,
            quantity=inventory.trading_available_to_rebuy,
            reason="rebuy conditions met",
        )


def apply_dry_run_fill(
    *,
    decision: CostReducerDecision,
    market: AdaptiveMarketState,
    inventory: InventoryState,
    state: CostReducerState,
    estimated_roundtrip_cost_bps: Decimal,
) -> None:
    if decision.action is CostReducerAction.SELL_TRADING:
        cost = (market.last_price * Decimal(decision.quantity) * estimated_roundtrip_cost_bps) / Decimal("10000")
        inventory.record_trading_sell(
            quantity=decision.quantity,
            price=market.last_price,
            estimated_cost=cost,
        )
        state.last_sell_price = market.last_price
        return

    if decision.action is CostReducerAction.REBUY_TRADING:
        cost = (market.last_price * Decimal(decision.quantity) * estimated_roundtrip_cost_bps) / Decimal("10000")
        inventory.record_trading_rebuy(
            quantity=decision.quantity,
            price=market.last_price,
            estimated_cost=cost,
        )
        state.round_trips_completed += 1
