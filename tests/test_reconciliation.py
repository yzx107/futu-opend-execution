from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.execution.orders import OrderRole, OrderSide
from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.services.reconciliation import FillRecord, InventoryManager


class ReconciliationTests(unittest.TestCase):
    def test_duplicate_fill_ignored_and_rebuy_restores_position(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        manager = InventoryManager(inventory)
        sell = FillRecord("order-1", OrderSide.SELL, OrderRole.TRADING_SELL, 100, "11", deal_id="deal-1")

        first = manager.apply_fill(sell)
        duplicate = manager.apply_fill(sell)
        manager.apply_fill(FillRecord("order-2", OrderSide.BUY, OrderRole.TRADING_REBUY, 100, "9.8", deal_id="deal-2"))

        self.assertEqual(first["event"], "real_fill_applied")
        self.assertEqual(duplicate["event"], "real_fill_duplicate_ignored")
        self.assertEqual(manager.inventory.current_position, 1000)
        self.assertEqual(manager.inventory.economic_cost_basis, (Decimal("10000") - Decimal("1100") + Decimal("980")) / Decimal("1000"))

    def test_fill_after_cancel_warns_and_applies_once(self) -> None:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        manager = InventoryManager(inventory)
        manager.mark_cancelled("order-1")

        warning = manager.apply_fill(FillRecord("order-1", OrderSide.SELL, OrderRole.TRADING_SELL, 100, "11", deal_id="deal-1"))
        duplicate = manager.apply_fill(FillRecord("order-1", OrderSide.SELL, OrderRole.TRADING_SELL, 100, "11", deal_id="deal-1"))

        self.assertEqual(warning["event"], "reconciliation_warning")
        self.assertEqual(duplicate["event"], "real_fill_duplicate_ignored")
        self.assertEqual(manager.inventory.trading_qty_sold, 100)


if __name__ == "__main__":
    unittest.main()
