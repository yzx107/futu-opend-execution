from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from futu_opend_execution.agent.runtime import TradingAgentConfig, build_inventory_for_existing_position, default_strategy
from futu_opend_execution.data.market import MarketState
from futu_opend_execution.services.cost_reducer import CostReducerAction, CostReducerExecutableStatus, CostReducerState


class CostReducerStrategyTests(unittest.TestCase):
    def _sell_state(self):
        return MarketState(
            symbol="HK.00700",
            timestamp=datetime(2026, 5, 21, 9, 30),
            interval_seconds=1,
            last_price=Decimal("10.25"),
            best_bid=Decimal("10.24"),
            bid_size=Decimal("100"),
            best_ask=Decimal("10.25"),
            ask_size=Decimal("500"),
            spread_bps=Decimal("5"),
            orderbook_imbalance=Decimal("-0.6"),
            opening_vwap=Decimal("10.00"),
            rolling_vwap=Decimal("10.00"),
            realized_vol=Decimal("0.10"),
            rolling_high=Decimal("10.35"),
            rolling_low=Decimal("9.95"),
            cumulative_volume=Decimal("1000"),
            cumulative_turnover=Decimal("10000"),
            volume_delta=Decimal("100"),
            turnover_delta=Decimal("1025"),
            tick_count=10,
            source="fixture",
        )

    def test_strategy_sells_existing_trading_bucket_only(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="10", lot_size=100)
        inventory = build_inventory_for_existing_position(config)

        intent = default_strategy(config).evaluate(market=self._sell_state(), inventory=inventory, state=CostReducerState())

        self.assertEqual(intent.action, CostReducerAction.SELL_TRADING)
        self.assertEqual(intent.quantity, 100)
        self.assertEqual(intent.status, CostReducerExecutableStatus.DRY_RUN_SIGNAL)
        self.assertIn("dry_run_only", intent.reason)
        self.assertEqual(inventory.current_position, 200)

    def test_strategy_rebuy_requires_prior_sell_and_cost_buffer(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="10", lot_size=100)
        inventory = build_inventory_for_existing_position(config)
        inventory.record_trading_sell(quantity=100, price="106")
        state = CostReducerState(last_sell_price=Decimal("10.30"))
        market = self._sell_state()
        object.__setattr__(market, "last_price", Decimal("10.10"))
        object.__setattr__(market, "best_ask", Decimal("10.10"))
        object.__setattr__(market, "orderbook_imbalance", Decimal("0.5"))

        intent = default_strategy(config).evaluate(market=market, inventory=inventory, state=state)

        self.assertEqual(intent.action, CostReducerAction.REBUY_TRADING)
        self.assertEqual(intent.quantity, 100)
        self.assertEqual(intent.status, CostReducerExecutableStatus.DRY_RUN_SIGNAL)
        self.assertIsNotNone(intent.limit_price)

    def test_spread_too_wide_is_not_executable(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="10", lot_size=100)
        inventory = build_inventory_for_existing_position(config)
        market = self._sell_state()
        object.__setattr__(market, "spread_bps", Decimal("25"))

        intent = default_strategy(config).evaluate(market=market, inventory=inventory, state=CostReducerState())

        self.assertEqual(intent.status, CostReducerExecutableStatus.NOT_EXECUTABLE)

    def test_stale_market_is_risk_blocked(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="10", lot_size=100)
        inventory = build_inventory_for_existing_position(config)
        market = self._sell_state()
        object.__setattr__(market, "stale", True)

        intent = default_strategy(config).evaluate(market=market, inventory=inventory, state=CostReducerState())

        self.assertEqual(intent.status, CostReducerExecutableStatus.RISK_BLOCKED)

    def test_no_previous_sell_blocks_rebuy(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="10", lot_size=100)
        inventory = build_inventory_for_existing_position(config)
        market = self._sell_state()
        object.__setattr__(market, "last_price", Decimal("10.10"))
        object.__setattr__(market, "best_ask", Decimal("10.10"))
        object.__setattr__(market, "orderbook_imbalance", Decimal("0.5"))

        intent = default_strategy(config).evaluate(market=market, inventory=inventory, state=CostReducerState())

        self.assertEqual(intent.status, CostReducerExecutableStatus.NOT_EXECUTABLE)

    def test_max_round_trips_reached_is_risk_blocked(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="10", lot_size=100, max_round_trips=0)
        inventory = build_inventory_for_existing_position(config)

        intent = default_strategy(config).evaluate(market=self._sell_state(), inventory=inventory, state=CostReducerState())

        self.assertEqual(intent.status, CostReducerExecutableStatus.RISK_BLOCKED)

    def test_max_sell_ratio_reached_is_risk_blocked(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=1000, cost_price="10", lot_size=100, max_sell_total_position_ratio="0.1")
        inventory = build_inventory_for_existing_position(config)
        inventory.record_trading_sell(quantity=100, price="10.5")

        intent = default_strategy(config).evaluate(market=self._sell_state(), inventory=inventory, state=CostReducerState())

        self.assertEqual(intent.status, CostReducerExecutableStatus.RISK_BLOCKED)


if __name__ == "__main__":
    unittest.main()
