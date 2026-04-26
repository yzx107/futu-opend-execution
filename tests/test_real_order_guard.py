from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.services.real_order import (
    GreyMarketRealOrderIntent,
    GreyOrderRole,
    GreyOrderSide,
    GreyOrderSource,
    RealOrderGuard,
)
from futu_opend_execution.strategy_config import ExecutionMode


class RealOrderGuardTests(unittest.TestCase):
    def _inventory(self):
        inventory = split_inventory_targets(total_quantity=1000, lot_size=100)
        inventory.seed_opening_inventory(anchor_price="10")
        inventory.record_trading_sell(quantity=200, price="10.5")
        return inventory

    def _guard(self, temp_dir: str, *, allow_real_trade: bool = True) -> RealOrderGuard:
        return RealOrderGuard(
            runtime_config=RuntimeConfig(allow_real_trade=allow_real_trade),
            kill_switch_file=Path(temp_dir) / "KILL",
            max_qty=1000,
            max_notional=Decimal("20000"),
            lot_size=100,
            max_order_attempts=5,
        )

    def _intent(
        self,
        *,
        side=GreyOrderSide.SELL,
        role=GreyOrderRole.TRADING_SELL,
        qty=100,
        price="10",
        client_id="intent-1",
    ) -> GreyMarketRealOrderIntent:
        return GreyMarketRealOrderIntent(
            symbol="HK.01234",
            side=side,
            quantity=qty,
            limit_price=price,
            role=role,
            source=GreyOrderSource.COST_REDUCER,
            client_intent_id=client_id,
        )

    def test_real_order_rejected_when_env_gate_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir, allow_real_trade=False)
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )

    def test_sell_rejected_if_role_is_core(self) -> None:
        with self.assertRaises(ExecutionValidationError):
            self._intent(role=GreyOrderRole.CORE_BUY)

    def test_sell_rejected_if_quantity_exceeds_trading_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(qty=800),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )

    def test_rebuy_rejected_if_quantity_exceeds_sold_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(
                        side=GreyOrderSide.BUY,
                        role=GreyOrderRole.TRADING_REBUY,
                        qty=300,
                    ),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_ask": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )

    def test_order_rejected_if_kill_switch_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            guard.kill_switch_file.write_text("stop", encoding="utf-8")
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )

    def test_order_rejected_if_notional_exceeds_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            guard.max_notional = Decimal("500")
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(qty=100, price="10"),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )

    def test_duplicate_client_intent_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            inventory = self._inventory()
            intent = self._intent(client_id="duplicate")
            guard.validate(
                intent,
                execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                inventory=inventory,
                market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                confirm_text="确认实盘",
                approved=True,
                now_monotonic=0.0,
            )
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    intent,
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=inventory,
                    market_snapshot={"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                    now_monotonic=1.0,
                )

    def test_cost_reducer_order_rejected_if_spread_too_wide(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"spread_bps": "30", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )

    def test_cost_reducer_order_rejected_if_market_snapshot_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            guard = self._guard(temp_dir)
            with self.assertRaises(ExecutionValidationError):
                guard.validate(
                    self._intent(),
                    execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                    inventory=self._inventory(),
                    market_snapshot={"stale": True, "spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
                    confirm_text="确认实盘",
                    approved=True,
                )


if __name__ == "__main__":
    unittest.main()
