from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.agent.risk import RealOrderGuard
from futu_opend_execution.execution.orders import OrderRole, OrderSide, RealOrderIntent
from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.strategy_config import ExecutionMode


class RiskGuardTests(unittest.TestCase):
    def _inventory(self):
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        inventory.record_trading_sell(quantity=100, price="11")
        return inventory

    def _intent(self, *, side=OrderSide.SELL, role=OrderRole.TRADING_SELL, qty=100, price="10") -> RealOrderIntent:
        return RealOrderIntent("HK.00700", side=side, quantity=qty, limit_price=price, role=role)

    def _validate(self, guard: RealOrderGuard, intent: RealOrderIntent) -> None:
        guard.validate(
            intent,
            execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
            inventory=self._inventory(),
            market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10", "best_ask": "10.1"},
            confirm_text="确认实盘",
            approved=True,
            now_monotonic=10,
        )

    def test_real_order_rejected_without_env_gate(self) -> None:
        guard = RealOrderGuard(allow_real_trade=True, max_qty=100, max_notional=Decimal("20000"), lot_size=100)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ExecutionValidationError, "FUTU_ALLOW_REAL_TRADE"):
                self._validate(guard, self._intent())

    def test_kill_switch_blocks_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            kill = Path(temp_dir) / "KILL"
            kill.write_text("stop", encoding="utf-8")
            guard = RealOrderGuard(allow_real_trade=True, kill_switch_file=kill, max_qty=100, max_notional=Decimal("20000"), lot_size=100)
            with self.assertRaisesRegex(ExecutionValidationError, "kill switch"):
                self._validate(guard, self._intent())

    def test_sell_core_and_exceed_inventory_are_blocked(self) -> None:
        with self.assertRaises(ValueError):
            self._intent(role=OrderRole.CORE_BUY)
        guard = RealOrderGuard(allow_real_trade=True, max_qty=1000, max_notional=Decimal("20000"), lot_size=100)
        with patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            with self.assertRaisesRegex(ExecutionValidationError, "trading inventory"):
                self._validate(guard, self._intent(qty=600))

    def test_auto_cost_reducer_remains_disabled_by_default(self) -> None:
        guard = RealOrderGuard(allow_real_trade=True, max_qty=100, max_notional=Decimal("20000"), lot_size=100)
        with patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            with self.assertRaisesRegex(ExecutionValidationError, "experimental auto"):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_AUTO,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )


if __name__ == "__main__":
    unittest.main()
