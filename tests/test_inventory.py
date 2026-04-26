from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.inventory import (
    InventoryValidationError,
    split_inventory_targets,
)


class InventoryTests(unittest.TestCase):
    def test_split_targets_50_50_by_lot(self) -> None:
        state = split_inventory_targets(total_quantity=1000, lot_size=100)
        self.assertEqual(state.core_qty_target, 500)
        self.assertEqual(state.trading_qty_target, 500)

    def test_sell_and_rebuy_constraints(self) -> None:
        state = split_inventory_targets(total_quantity=1000, lot_size=100)
        state.seed_opening_inventory(anchor_price="10")

        with self.assertRaises(InventoryValidationError):
            state.record_trading_sell(quantity=600, price="10.1")

        state.record_trading_sell(quantity=200, price="10.2")
        self.assertEqual(state.trading_available_to_sell, 300)

        with self.assertRaises(InventoryValidationError):
            state.record_trading_rebuy(quantity=300, price="9.8")

        state.record_trading_rebuy(quantity=200, price="9.8")
        self.assertEqual(state.trading_available_to_rebuy, 0)
        self.assertGreater(state.economic_net_cost, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
