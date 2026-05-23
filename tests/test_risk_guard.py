from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.agent.risk import RealOrderGuard
from futu_opend_execution.config import RuntimeConfig
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

    def test_confirmation_phrase_and_non_loopback_host_rejected(self) -> None:
        guard = RealOrderGuard(allow_real_trade=True, max_qty=100, max_notional=Decimal("20000"), lot_size=100)
        with patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            with self.assertRaisesRegex(ExecutionValidationError, "confirmation phrase"):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10", "best_ask": "10.1"},
                    confirm_text="wrong",
                    approved=True,
                )
            with self.assertRaisesRegex(ExecutionValidationError, "loopback"):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10", "best_ask": "10.1"},
                    runtime_config=RuntimeConfig(futu_host="192.168.1.10"),
                    confirm_text="确认实盘",
                    approved=True,
                    reserve=False,
                )

    def test_quantity_notional_lot_and_market_guards(self) -> None:
        with patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            with self.assertRaisesRegex(ExecutionValidationError, "max_qty"):
                self._validate(RealOrderGuard(allow_real_trade=True, max_qty=50, max_notional=Decimal("20000"), lot_size=100), self._intent())
            with self.assertRaisesRegex(ExecutionValidationError, "notional"):
                self._validate(RealOrderGuard(allow_real_trade=True, max_qty=100, max_notional=Decimal("999"), lot_size=100), self._intent(price="10"))
            with self.assertRaisesRegex(ExecutionValidationError, "lot-aligned"):
                self._validate(RealOrderGuard(allow_real_trade=True, max_qty=100, max_notional=Decimal("20000"), lot_size=100), self._intent(qty=50))

            guard = RealOrderGuard(allow_real_trade=True, max_qty=100, max_notional=Decimal("20000"), lot_size=100)
            with self.assertRaisesRegex(ExecutionValidationError, "stale"):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"stale": True, "spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                    reserve=False,
                )
            with self.assertRaisesRegex(ExecutionValidationError, "spread too wide"):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "21", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                    reserve=False,
                )

    def test_sell_core_and_exceed_inventory_are_blocked(self) -> None:
        with self.assertRaises(ValueError):
            self._intent(role=OrderRole.CORE_BUY)
        guard = RealOrderGuard(allow_real_trade=True, max_qty=1000, max_notional=Decimal("20000"), lot_size=100)
        with patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            with self.assertRaisesRegex(ExecutionValidationError, "trading inventory"):
                self._validate(guard, self._intent(qty=600))

    def test_rebuy_role_inventory_duplicate_and_rate_limits(self) -> None:
        with patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            guard = RealOrderGuard(allow_real_trade=True, max_qty=200, max_notional=Decimal("20000"), lot_size=100)
            with self.assertRaisesRegex(ExecutionValidationError, "sold trading inventory"):
                self._validate(guard, self._intent(side=OrderSide.BUY, role=OrderRole.TRADING_REBUY, qty=200))

            duplicate = self._intent()
            self._validate(guard, duplicate)
            with self.assertRaisesRegex(ExecutionValidationError, "duplicate client_intent_id"):
                guard.validate(
                    duplicate,
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10", "best_ask": "10.1"},
                    confirm_text="确认实盘",
                    approved=True,
                    now_monotonic=20,
                )

            cooldown_guard = RealOrderGuard(
                allow_real_trade=True,
                max_qty=100,
                max_notional=Decimal("20000"),
                lot_size=100,
                max_order_attempts=2,
                min_interval_seconds=5,
            )
            self._validate(cooldown_guard, self._intent())
            with self.assertRaisesRegex(ExecutionValidationError, "cooldown"):
                cooldown_guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10", "best_ask": "10.1"},
                    confirm_text="确认实盘",
                    approved=True,
                    now_monotonic=10.5,
                )

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
