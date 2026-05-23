"""Dry-run-first cost reducer decision engine for existing HK positions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.execution.orders import OrderRole, OrderSide
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


class CostReducerExecutableStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class CostReducerExecutionPolicy:
    dry_run_only: bool = True
    manual_approval_required: bool = True
    enable_real_sell: bool = False
    enable_real_rebuy: bool = False
    enable_auto_cost_reducer: bool = False
    max_real_sell_qty: int = 0
    max_real_rebuy_qty: int = 0
    max_real_sell_notional: Decimal = Decimal("0")
    max_real_rebuy_notional: Decimal = Decimal("0")
    max_cost_reducer_orders_per_session: int = 1
    min_seconds_between_cost_reducer_orders: Decimal = Decimal("5")
    require_positive_expected_edge: bool = True
    sell_limit_offset_ticks: int = 0
    rebuy_limit_offset_ticks: int = 0
    min_sell_price: Decimal | None = None
    max_rebuy_price: Decimal | None = None
    original_max_price: Decimal | None = None
    max_sell_slippage_bps: Decimal = Decimal("20")
    max_rebuy_slippage_bps: Decimal = Decimal("20")
    min_expected_edge_bps: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class CostReducerExecutableIntent:
    action: CostReducerAction
    side: OrderSide | None
    role: OrderRole | None
    quantity: int
    reference_price: Decimal | None
    limit_price: Decimal | None
    reason: str
    expected_edge_bps: Decimal
    estimated_cost_bps: Decimal
    safety_buffer_bps: Decimal
    market_snapshot: dict
    inventory_snapshot: dict
    approved: bool = False
    status: CostReducerExecutableStatus = CostReducerExecutableStatus.PENDING_APPROVAL


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

        net_trading_sold = inventory.trading_qty_sold - inventory.trading_qty_rebought
        max_sell_by_ratio = int(
            Decimal(inventory.total_target_qty) * self._rules.max_sell_total_position_ratio
        )
        remaining_sell_capacity = max_sell_by_ratio - net_trading_sold
        if remaining_sell_capacity <= 0:
            return CostReducerDecision(CostReducerAction.BLOCK, reason="max cumulative sell ratio reached")

        quantity = min(inventory.trading_available_to_sell, remaining_sell_capacity)
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


def build_executable_intent(
    *,
    decision: CostReducerDecision,
    market: AdaptiveMarketState,
    inventory: InventoryState,
    rules: CostReducerRules,
    policy: CostReducerExecutionPolicy,
    best_bid: Decimal | str | int | float | None,
    best_ask: Decimal | str | int | float | None,
    last_sell_price: Decimal | str | int | float | None = None,
) -> CostReducerExecutableIntent:
    if decision.action not in {
        CostReducerAction.SELL_TRADING,
        CostReducerAction.REBUY_TRADING,
    }:
        return _blocked_intent(
            decision=decision,
            market=market,
            inventory=inventory,
            rules=rules,
            reason=decision.reason or "not executable",
        )
    if market.spread_bps > rules.max_spread_bps:
        return _blocked_intent(
            decision=decision,
            market=market,
            inventory=inventory,
            rules=rules,
            reason="spread too wide",
        )

    if decision.action is CostReducerAction.SELL_TRADING:
        bid = _to_decimal_optional(best_bid)
        if bid is None or bid <= 0:
            return _blocked_intent(
                decision=decision,
                market=market,
                inventory=inventory,
                rules=rules,
                reason="best_bid unavailable",
            )
        limit_price = bid - Decimal(policy.sell_limit_offset_ticks) * policy.tick_size
        if policy.min_sell_price is not None:
            limit_price = max(limit_price, policy.min_sell_price)
        if market.last_price is not None and market.last_price > 0:
            floor = market.last_price * (
                Decimal("1") - policy.max_sell_slippage_bps / Decimal("10000")
            )
            limit_price = max(limit_price, floor)
        expected_edge = _bps(limit_price - inventory.economic_cost_basis, inventory.economic_cost_basis)
        return _executable_intent(
            decision=decision,
            market=market,
            inventory=inventory,
            rules=rules,
            policy=policy,
            side=OrderSide.SELL,
            role=OrderRole.TRADING_SELL,
            reference_price=bid,
            limit_price=limit_price,
            expected_edge_bps=expected_edge,
        )

    ask = _to_decimal_optional(best_ask)
    if ask is None or ask <= 0:
        return _blocked_intent(
            decision=decision,
            market=market,
            inventory=inventory,
            rules=rules,
            reason="best_ask unavailable",
        )
    sell_anchor = _to_decimal_optional(last_sell_price)
    if sell_anchor is None or sell_anchor <= 0:
        return _blocked_intent(
            decision=decision,
            market=market,
            inventory=inventory,
            rules=rules,
            reason="last_sell_price unavailable",
        )
    limit_price = ask + Decimal(policy.rebuy_limit_offset_ticks) * policy.tick_size
    caps = [cap for cap in (policy.max_rebuy_price, policy.original_max_price) if cap is not None and cap > 0]
    total_cost_bps = rules.estimated_roundtrip_cost_bps + rules.safety_buffer_bps
    if caps:
        limit_price = min(limit_price, *caps)
    max_profitable_price = sell_anchor * (
        Decimal("1") - total_cost_bps / Decimal("10000")
    )
    limit_price = min(limit_price, max_profitable_price)
    expected_edge = _bps(sell_anchor - limit_price, sell_anchor) - total_cost_bps
    return _executable_intent(
        decision=decision,
        market=market,
        inventory=inventory,
        rules=rules,
        policy=policy,
        side=OrderSide.BUY,
        role=OrderRole.TRADING_REBUY,
        reference_price=ask,
        limit_price=limit_price,
        expected_edge_bps=expected_edge,
    )


def _executable_intent(
    *,
    decision: CostReducerDecision,
    market: AdaptiveMarketState,
    inventory: InventoryState,
    rules: CostReducerRules,
    policy: CostReducerExecutionPolicy,
    side: OrderSide,
    role: OrderRole,
    reference_price: Decimal,
    limit_price: Decimal,
    expected_edge_bps: Decimal,
) -> CostReducerExecutableIntent:
    reason = decision.reason
    status = CostReducerExecutableStatus.PENDING_APPROVAL
    if policy.dry_run_only:
        status = CostReducerExecutableStatus.BLOCKED
        reason = f"{reason}; dry_run_only"
    if policy.require_positive_expected_edge and expected_edge_bps < policy.min_expected_edge_bps:
        return _blocked_intent(
            decision=decision,
            market=market,
            inventory=inventory,
            rules=rules,
            reason=f"{reason}; expected edge below threshold",
        )
    return CostReducerExecutableIntent(
        action=decision.action,
        side=side,
        role=role,
        quantity=decision.quantity,
        reference_price=reference_price,
        limit_price=limit_price,
        reason=reason,
        expected_edge_bps=expected_edge_bps,
        estimated_cost_bps=rules.estimated_roundtrip_cost_bps,
        safety_buffer_bps=rules.safety_buffer_bps,
        market_snapshot=_market_snapshot(market),
        inventory_snapshot=_inventory_snapshot(inventory),
        approved=False,
        status=status,
    )


def _blocked_intent(
    *,
    decision: CostReducerDecision,
    market: AdaptiveMarketState,
    inventory: InventoryState,
    rules: CostReducerRules,
    reason: str,
) -> CostReducerExecutableIntent:
    return CostReducerExecutableIntent(
        action=decision.action,
        side=None,
        role=None,
        quantity=decision.quantity,
        reference_price=market.last_price,
        limit_price=None,
        reason=reason,
        expected_edge_bps=Decimal("0"),
        estimated_cost_bps=rules.estimated_roundtrip_cost_bps,
        safety_buffer_bps=rules.safety_buffer_bps,
        market_snapshot=_market_snapshot(market),
        inventory_snapshot=_inventory_snapshot(inventory),
        status=CostReducerExecutableStatus.BLOCKED,
    )


def _market_snapshot(market: AdaptiveMarketState) -> dict:
    return {
        "last_price": market.last_price,
        "opening_vwap": market.opening_vwap,
        "rolling_vwap": market.rolling_vwap,
        "realized_vol": market.realized_vol,
        "rolling_high": market.rolling_high,
        "rolling_low": market.rolling_low,
        "orderbook_imbalance": market.orderbook_imbalance,
        "spread_bps": market.spread_bps,
        "tick_count": market.tick_count,
        "cumulative_turnover": market.cumulative_turnover,
    }


def _inventory_snapshot(inventory: InventoryState) -> dict:
    return {
        "current_position": inventory.current_position,
        "trading_available_to_sell": inventory.trading_available_to_sell,
        "trading_available_to_rebuy": inventory.trading_available_to_rebuy,
        "economic_cost_basis": inventory.economic_cost_basis,
        "trading_qty_sold": inventory.trading_qty_sold,
        "trading_qty_rebought": inventory.trading_qty_rebought,
    }


def _to_decimal_optional(value: Decimal | str | int | float | None) -> Decimal | None:
    if value in {None, ""}:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _bps(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (numerator / denominator) * Decimal("10000")
