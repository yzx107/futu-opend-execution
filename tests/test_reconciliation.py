from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.services.real_order import GreyOrderRole, GreyOrderSide
from futu_opend_execution.services.reconciliation import FillRecord, InventoryManager


class ReconciliationTests(unittest.TestCase):
    def _manager(self) -> InventoryManager:
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        return InventoryManager(inventory)

    def test_partial_fill_updates_inventory_partially(self) -> None:
        manager = self._manager()
        manager.inventory.core_qty_filled = 0
        manager.apply_fill(
            FillRecord(
                order_id="1",
                side=GreyOrderSide.BUY,
                role=GreyOrderRole.CORE_BUY,
                quantity=200,
                price="10",
                deal_id="d1",
            )
        )
        self.assertEqual(manager.inventory.core_qty_filled, 200)

    def test_duplicate_fill_ignored(self) -> None:
        manager = self._manager()
        fill = FillRecord(
            order_id="1",
            side=GreyOrderSide.SELL,
            role=GreyOrderRole.TRADING_SELL,
            quantity=100,
            price="11",
            deal_id="d1",
        )
        first = manager.apply_fill(fill)
        second = manager.apply_fill(fill)
        self.assertEqual(first["event"], "real_fill_applied")
        self.assertEqual(second["event"], "real_fill_duplicate_ignored")
        self.assertEqual(manager.inventory.trading_qty_sold, 100)

    def test_cancelled_order_does_not_update_inventory(self) -> None:
        manager = self._manager()
        manager.mark_cancelled("1")
        event = manager.apply_fill(
            FillRecord(
                order_id="1",
                side=GreyOrderSide.SELL,
                role=GreyOrderRole.TRADING_SELL,
                quantity=100,
                price="11",
            )
        )
        self.assertEqual(event["event"], "inventory_reconciliation_warning")
        self.assertEqual(manager.inventory.trading_qty_sold, 0)

    def test_sell_fill_reduces_trading_inventory_only(self) -> None:
        manager = self._manager()
        manager.apply_fill(
            FillRecord(
                order_id="1",
                side=GreyOrderSide.SELL,
                role=GreyOrderRole.TRADING_SELL,
                quantity=100,
                price="11",
            )
        )
        self.assertEqual(manager.inventory.core_qty_filled, 500)
        self.assertEqual(manager.inventory.trading_qty_sold, 100)
        self.assertEqual(manager.inventory.current_position, 900)

    def test_rebuy_fill_increases_position_and_updates_cost_basis(self) -> None:
        manager = self._manager()
        manager.apply_fill(
            FillRecord(
                order_id="1",
                side=GreyOrderSide.SELL,
                role=GreyOrderRole.TRADING_SELL,
                quantity=100,
                price="11",
            )
        )
        manager.apply_fill(
            FillRecord(
                order_id="2",
                side=GreyOrderSide.BUY,
                role=GreyOrderRole.TRADING_REBUY,
                quantity=100,
                price="9.8",
            )
        )
        self.assertEqual(manager.inventory.current_position, 1000)
        expected_basis = (Decimal("10000") - Decimal("1100") + Decimal("980")) / Decimal("1000")
        self.assertEqual(manager.inventory.economic_cost_basis, expected_basis)


if __name__ == "__main__":
    unittest.main()
