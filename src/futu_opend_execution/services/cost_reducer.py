"""Dry-run/replay-only grey-market cost reducer decision engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.services.real_order import GreyOrderRole, GreyOrderSide
from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState

from futu_opend_execution.signals.market_pressure import MarketPressureCalculator
from futu_opend_execution.services.trading_position import TradingPositionEngine, TradingPositionRules, TradingAction
import time

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
    side: GreyOrderSide | None
    role: GreyOrderRole | None
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
        self._tp_engine = None
        self._pressure_calc = MarketPressureCalculator()
        self._start_time = None

    def evaluate(
        self,
        *,
        inventory: InventoryState,
        market: AdaptiveMarketState,
        state: CostReducerState,
    ) -> CostReducerDecision:
        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
            
        if self._tp_engine is None:
            tp_rules = TradingPositionRules()
            self._tp_engine = TradingPositionEngine(rules=tp_rules, total_trading_qty=inventory.trading_qty_target, lot_size=1)
            
        elapsed = now - self._start_time
        pressure = self._pressure_calc.compute(market, elapsed_seconds=elapsed)
        decision = self._tp_engine.evaluate(market, pressure)
        
        if decision.action == TradingAction.BUY_TRADING:
            return CostReducerDecision(CostReducerAction.REBUY_TRADING, quantity=decision.quantity, reason=decision.reason)
        elif decision.action == TradingAction.SELL_TRADING:
            return CostReducerDecision(CostReducerAction.SELL_TRADING, quantity=decision.quantity, reason=decision.reason)
        elif decision.action == TradingAction.SELL_ALL:
            return CostReducerDecision(CostReducerAction.SELL_TRADING, quantity=decision.quantity, reason=decision.reason)
            
        return CostReducerDecision(CostReducerAction.WAIT, reason=decision.reason)

    def _evaluate_sell(self, *args, **kwargs): pass
    def _evaluate_rebuy(self, *args, **kwargs): pass


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
            side=GreyOrderSide.SELL,
            role=GreyOrderRole.TRADING_SELL,
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
    limit_price = ask + Decimal(policy.rebuy_limit_offset_ticks) * policy.tick_size
    caps = [cap for cap in (policy.max_rebuy_price, policy.original_max_price) if cap is not None and cap > 0]
    total_cost_bps = rules.estimated_roundtrip_cost_bps + rules.safety_buffer_bps
    if caps:
        limit_price = min(limit_price, *caps)
    expected_edge = Decimal("0")
    if rules.estimated_roundtrip_cost_bps >= 0 and rules.safety_buffer_bps >= 0:
        # Rebuy edge is measured against the last real/dry-run trading sell price.
        # If that anchor is not available, the caller should keep the intent blocked upstream.
        expected_edge = Decimal("0")
    max_profitable_price = None
    if market.last_price is not None and market.last_price > 0:
        max_profitable_price = market.last_price * (
            Decimal("1") - total_cost_bps / Decimal("10000")
        )
        limit_price = min(limit_price, max_profitable_price)
    return _executable_intent(
        decision=decision,
        market=market,
        inventory=inventory,
        rules=rules,
        policy=policy,
        side=GreyOrderSide.BUY,
        role=GreyOrderRole.TRADING_REBUY,
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
    side: GreyOrderSide,
    role: GreyOrderRole,
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
        status = CostReducerExecutableStatus.BLOCKED
        reason = f"{reason}; expected edge below threshold"
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
