from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerEngine,
    CostReducerRules,
    CostReducerState,
)
from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState


class CostReducerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
