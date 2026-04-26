from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerEngine,
    CostReducerExecutableStatus,
    CostReducerExecutionPolicy,
    CostReducerRules,
    CostReducerState,
    build_executable_intent,
)
from futu_opend_execution.services.real_order import GreyOrderRole, GreyOrderSide
from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState


class CostReducerTests(unittest.TestCase):
    def _market(
        self,
        *,
        last_price: Decimal = Decimal("10.25"),
        rolling_high: Decimal = Decimal("10.35"),
        opening_vwap: Decimal = Decimal("10.00"),
        orderbook_imbalance: Decimal = Decimal("-0.2"),
        spread_bps: Decimal = Decimal("8"),
    ) -> AdaptiveMarketState:
        return AdaptiveMarketState(
            opening_vwap=opening_vwap,
            rolling_vwap=Decimal("10.10"),
            realized_vol=Decimal("0.1"),
            rolling_high=rolling_high,
            rolling_low=Decimal("9.9"),
            cumulative_turnover=Decimal("1000000"),
            volume_delta=Decimal("1000"),
            turnover_delta=Decimal("10000"),
            cumulative_field_reset_detected=False,
            tick_count=10,
            orderbook_imbalance=orderbook_imbalance,
            spread_bps=spread_bps,
            last_price=last_price,
        )

    def test_sell_then_rebuy_flow(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")

        engine = CostReducerEngine(
            CostReducerRules(
                min_ticks_to_activate=1,
                max_spread_bps=Decimal("30"),
                estimated_roundtrip_cost_bps=Decimal("10"),
                safety_buffer_bps=Decimal("5"),
            )
        )
        state = CostReducerState(last_sell_price=Decimal("10.30"))

        sell_market = AdaptiveMarketState(
            opening_vwap=Decimal("10.00"),
            rolling_vwap=Decimal("10.10"),
            realized_vol=Decimal("0.1"),
            rolling_high=Decimal("10.35"),
            rolling_low=Decimal("9.9"),
            cumulative_turnover=Decimal("1000000"),
            volume_delta=Decimal("1000"),
            turnover_delta=Decimal("10000"),
            cumulative_field_reset_detected=False,
            tick_count=10,
            orderbook_imbalance=Decimal("-0.2"),
            spread_bps=Decimal("8"),
            last_price=Decimal("10.25"),
        )
        sell_decision = engine.evaluate(inventory=inventory, market=sell_market, state=CostReducerState())
        self.assertEqual(sell_decision.action, CostReducerAction.SELL_TRADING)
        self.assertGreater(sell_decision.quantity, 0)

        inventory.record_trading_sell(quantity=sell_decision.quantity, price=sell_market.last_price)

        rebuy_market = AdaptiveMarketState(
            opening_vwap=Decimal("10.00"),
            rolling_vwap=Decimal("10.01"),
            realized_vol=Decimal("0.1"),
            rolling_high=Decimal("10.35"),
            rolling_low=Decimal("9.8"),
            cumulative_turnover=Decimal("1200000"),
            volume_delta=Decimal("1000"),
            turnover_delta=Decimal("10000"),
            cumulative_field_reset_detected=False,
            tick_count=20,
            orderbook_imbalance=Decimal("0.2"),
            spread_bps=Decimal("6"),
            last_price=Decimal("10.10"),
        )
        rebuy_decision = engine.evaluate(
            inventory=inventory,
            market=rebuy_market,
            state=state,
        )
        self.assertEqual(rebuy_decision.action, CostReducerAction.REBUY_TRADING)

    def test_rebought_trading_inventory_respects_round_trip_limit(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        state = CostReducerState(last_sell_price=Decimal("10.30"))
        engine = CostReducerEngine(CostReducerRules(min_ticks_to_activate=1, max_round_trips=1))

        sell = engine.evaluate(inventory=inventory, market=self._market(), state=state)
        self.assertEqual(sell.action, CostReducerAction.SELL_TRADING)
        inventory.record_trading_sell(quantity=200, price="10.25")

        rebuy_market = self._market(
            last_price=Decimal("10.10"),
            rolling_high=Decimal("10.35"),
            orderbook_imbalance=Decimal("0.2"),
        )
        rebuy = engine.evaluate(inventory=inventory, market=rebuy_market, state=state)
        self.assertEqual(rebuy.action, CostReducerAction.REBUY_TRADING)
        inventory.record_trading_rebuy(quantity=200, price="10.10")
        state.round_trips_completed += 1

        blocked = engine.evaluate(inventory=inventory, market=self._market(), state=state)
        self.assertEqual(blocked.action, CostReducerAction.BLOCK)
        self.assertIn("max_round_trips", blocked.reason)

    def test_repeated_sells_cannot_exceed_cumulative_max_sell_ratio(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        engine = CostReducerEngine(
            CostReducerRules(
                min_ticks_to_activate=1,
                max_sell_total_position_ratio=Decimal("0.2"),
                max_round_trips=5,
            )
        )
        state = CostReducerState()

        first = engine.evaluate(inventory=inventory, market=self._market(), state=state)
        self.assertEqual(first.action, CostReducerAction.SELL_TRADING)
        self.assertEqual(first.quantity, 200)
        inventory.record_trading_sell(quantity=first.quantity, price="10.25")

        blocked = engine.evaluate(inventory=inventory, market=self._market(), state=state)
        self.assertEqual(blocked.action, CostReducerAction.BLOCK)
        self.assertIn("max cumulative sell ratio", blocked.reason)

    def test_wide_spread_blocks_sell(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        engine = CostReducerEngine(CostReducerRules(min_ticks_to_activate=1))

        decision = engine.evaluate(
            inventory=inventory,
            market=self._market(spread_bps=Decimal("21")),
            state=CostReducerState(),
        )

        self.assertEqual(decision.action, CostReducerAction.WAIT)
        self.assertIn("spread", decision.reason)

    def test_high_price_without_pullback_blocks_sell(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        engine = CostReducerEngine(CostReducerRules(min_ticks_to_activate=1))

        decision = engine.evaluate(
            inventory=inventory,
            market=self._market(last_price=Decimal("10.31"), rolling_high=Decimal("10.34")),
            state=CostReducerState(),
        )

        self.assertEqual(decision.action, CostReducerAction.WAIT)

    def test_rebuy_does_not_trigger_without_cost_buffer(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        inventory.record_trading_sell(quantity=200, price="10.30")
        engine = CostReducerEngine(
            CostReducerRules(
                min_ticks_to_activate=1,
                estimated_roundtrip_cost_bps=Decimal("20"),
                safety_buffer_bps=Decimal("10"),
            )
        )

        decision = engine.evaluate(
            inventory=inventory,
            market=self._market(last_price=Decimal("10.28"), orderbook_imbalance=Decimal("0.2")),
            state=CostReducerState(last_sell_price=Decimal("10.30")),
        )

        self.assertEqual(decision.action, CostReducerAction.WAIT)

    def test_rebuy_requires_near_anchor_and_recovered_imbalance(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        inventory.record_trading_sell(quantity=200, price="10.30")
        engine = CostReducerEngine(CostReducerRules(min_ticks_to_activate=1))
        state = CostReducerState(last_sell_price=Decimal("10.30"))

        far_from_anchor = engine.evaluate(
            inventory=inventory,
            market=self._market(
                last_price=Decimal("10.10"),
                opening_vwap=Decimal("9.80"),
                orderbook_imbalance=Decimal("0.2"),
            ),
            state=state,
        )
        self.assertEqual(far_from_anchor.action, CostReducerAction.WAIT)

        weak_imbalance = engine.evaluate(
            inventory=inventory,
            market=self._market(last_price=Decimal("10.10"), orderbook_imbalance=Decimal("0")),
            state=state,
        )
        self.assertEqual(weak_imbalance.action, CostReducerAction.WAIT)

        recovered = engine.evaluate(
            inventory=inventory,
            market=self._market(last_price=Decimal("10.10"), orderbook_imbalance=Decimal("0.2")),
            state=state,
        )
        self.assertEqual(recovered.action, CostReducerAction.REBUY_TRADING)

    def test_sell_decision_creates_sell_trading_executable_intent(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        rules = CostReducerRules(min_ticks_to_activate=1)
        decision = CostReducerDecisionLike(CostReducerAction.SELL_TRADING, 100, "sell")

        intent = build_executable_intent(
            decision=decision,
            market=self._market(last_price=Decimal("10.5")),
            inventory=inventory,
            rules=rules,
            policy=CostReducerExecutionPolicy(
                dry_run_only=False,
                require_positive_expected_edge=False,
                sell_limit_offset_ticks=1,
                max_sell_slippage_bps=Decimal("100"),
            ),
            best_bid=Decimal("10.48"),
            best_ask=Decimal("10.50"),
        )

        self.assertEqual(intent.side, GreyOrderSide.SELL)
        self.assertEqual(intent.role, GreyOrderRole.TRADING_SELL)
        self.assertEqual(intent.limit_price, Decimal("10.47"))
        self.assertEqual(intent.status, CostReducerExecutableStatus.PENDING_APPROVAL)

    def test_rebuy_decision_creates_buy_rebuy_executable_intent(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        inventory.record_trading_sell(quantity=100, price="10.5")
        rules = CostReducerRules(min_ticks_to_activate=1)
        decision = CostReducerDecisionLike(CostReducerAction.REBUY_TRADING, 100, "rebuy")

        intent = build_executable_intent(
            decision=decision,
            market=self._market(last_price=Decimal("10.1"), orderbook_imbalance=Decimal("0.3")),
            inventory=inventory,
            rules=rules,
            policy=CostReducerExecutionPolicy(
                dry_run_only=False,
                require_positive_expected_edge=False,
                rebuy_limit_offset_ticks=1,
            ),
            best_bid=Decimal("10.09"),
            best_ask=Decimal("10.10"),
        )

        self.assertEqual(intent.side, GreyOrderSide.BUY)
        self.assertEqual(intent.role, GreyOrderRole.TRADING_REBUY)
        self.assertLessEqual(intent.limit_price, Decimal("10.11"))
        self.assertIsNone(getattr(intent, "order_type", None))

    def test_dry_run_policy_blocks_real_execution_before_approval(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")

        intent = build_executable_intent(
            decision=CostReducerDecisionLike(CostReducerAction.SELL_TRADING, 100, "sell"),
            market=self._market(last_price=Decimal("10.5")),
            inventory=inventory,
            rules=CostReducerRules(min_ticks_to_activate=1),
            policy=CostReducerExecutionPolicy(dry_run_only=True),
            best_bid=Decimal("10.48"),
            best_ask=Decimal("10.50"),
        )

        self.assertEqual(intent.status, CostReducerExecutableStatus.BLOCKED)
        self.assertFalse(intent.approved)


class CostReducerDecisionLike:
    def __init__(self, action, quantity, reason) -> None:
        self.action = action
        self.quantity = quantity
        self.reason = reason


if __name__ == "__main__":
    unittest.main()
