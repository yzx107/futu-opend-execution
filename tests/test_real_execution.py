from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.agent.approval import PendingRealOrderApproval
from futu_opend_execution.agent.real_execution import RealExecutionService
from futu_opend_execution.agent.risk import RealOrderGuard
from futu_opend_execution.execution.orders import OrderRole, OrderSide
from futu_opend_execution.models import BrokerOrderSnapshot, BrokerOrderStatus, TradeMode


def _approval_payload(*, side="SELL", role="TRADING_SELL", inventory=None):
    if inventory is None:
        inventory = {
            "trading_available_to_sell": 100,
            "trading_available_to_rebuy": 0,
        }
    return {
        "approval_id": f"approval-{side.lower()}",
        "signal_id": "signal-1",
        "symbol": "HK.00700",
        "side": side,
        "role": role,
        "quantity": 100,
        "limit_price": "300.00",
        "expected_edge_bps": "50",
        "created_at": "2026-05-23T09:30:00+00:00",
        "expires_at": "2099-05-23T09:35:00+00:00",
        "approved": True,
        "approved_by_operator": "operator",
        "confirmation_phrase": "确认实盘",
        "source_signal_status": "DRY_RUN_SIGNAL",
        "lot_size": 100,
        "market_snapshot": {
            "stale": False,
            "spread_bps": "5",
            "max_spread_bps": "20",
            "best_bid": "300.00",
            "best_ask": "300.20",
        },
        "inventory_snapshot": inventory,
        "risk_snapshot": {"max_severity": "INFO", "has_critical": False},
    }


def _approval(**kwargs) -> PendingRealOrderApproval:
    return PendingRealOrderApproval.from_dict(_approval_payload(**kwargs))


def _snapshot(
    status: BrokerOrderStatus,
    *,
    dealt_quantity: int = 0,
    order_id: str = "order-1",
) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        order_id=order_id,
        symbol="HK.00700",
        status=status,
        quantity=100,
        price=Decimal("300.00"),
        dealt_quantity=dealt_quantity,
        dealt_avg_price=Decimal("300.00") if dealt_quantity else None,
        updated_time="2026-05-23 09:31:00",
    )


class FakeBroker:
    supports_native_ioc = False

    def __init__(self, initial: BrokerOrderSnapshot, *, after_cancel: BrokerOrderSnapshot | None = None) -> None:
        self.initial = initial
        self.after_cancel = after_cancel or initial
        self.cancelled = False
        self.placed_side: OrderSide | None = None

    def place_limit_sell(self, **kwargs) -> BrokerOrderSnapshot:
        self.placed_side = OrderSide.SELL
        self._assert_real_limit(kwargs)
        return self.initial

    def place_limit_buy(self, **kwargs) -> BrokerOrderSnapshot:
        self.placed_side = OrderSide.BUY
        self._assert_real_limit(kwargs)
        return self.initial

    def get_order(self, *, order_id: str, symbol: str, trade_mode: TradeMode) -> BrokerOrderSnapshot:
        del order_id, symbol, trade_mode
        return self.after_cancel if self.cancelled else self.initial

    def cancel_order(self, *, order_id: str, symbol: str, trade_mode: TradeMode) -> None:
        del order_id, symbol, trade_mode
        self.cancelled = True

    def close(self) -> None:
        return None

    def _assert_real_limit(self, kwargs) -> None:
        self.last_kwargs = kwargs
        assert kwargs["trade_mode"] is TradeMode.REAL
        assert kwargs["limit_price"] == Decimal("300.00")


class RealExecutionTests(unittest.TestCase):
    def _service(self, broker: FakeBroker, audit_log: Path) -> RealExecutionService:
        return RealExecutionService(
            broker=broker,
            guard=RealOrderGuard(
                allow_real_trade=True,
                max_qty=100,
                max_notional=Decimal("30000"),
                lot_size=100,
            ),
            audit_log_path=audit_log,
            timeout_seconds=0,
            poll_interval_seconds=0,
        )

    def _events(self, audit_log: Path) -> list[dict]:
        return [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]

    def test_successful_limit_sell_and_rebuy_use_fake_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            sell_log = Path(temp_dir) / "sell.jsonl"
            sell_service = self._service(FakeBroker(_snapshot(BrokerOrderStatus.FILLED_ALL, dealt_quantity=100)), sell_log)
            sell_summary = sell_service.submit_approval(_approval(), confirm_text="确认实盘")

            rebuy_log = Path(temp_dir) / "rebuy.jsonl"
            rebuy = _approval(side="BUY", role="TRADING_REBUY", inventory={"trading_available_to_sell": 0, "trading_available_to_rebuy": 100})
            rebuy_broker = FakeBroker(_snapshot(BrokerOrderStatus.FILLED_ALL, dealt_quantity=100))
            rebuy_service = self._service(rebuy_broker, rebuy_log)
            rebuy_summary = rebuy_service.submit_approval(rebuy, confirm_text="确认实盘")

        self.assertTrue(sell_summary.ok)
        self.assertEqual(sell_summary.inventory["trading_qty_sold"], 100)
        self.assertEqual(rebuy_broker.placed_side, OrderSide.BUY)
        self.assertTrue(rebuy_summary.ok)
        self.assertEqual(rebuy_summary.inventory["trading_qty_rebought"], 100)

    def test_timeout_cancelled_unfilled_does_not_update_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            audit_log = Path(temp_dir) / "audit.jsonl"
            broker = FakeBroker(
                _snapshot(BrokerOrderStatus.SUBMITTED),
                after_cancel=_snapshot(BrokerOrderStatus.CANCELLED_ALL),
            )
            summary = self._service(broker, audit_log).submit_approval(_approval(), confirm_text="确认实盘")
            events = [row["event"] for row in self._events(audit_log)]

        self.assertTrue(broker.cancelled)
        self.assertFalse(summary.ok)
        self.assertEqual(summary.inventory["trading_qty_sold"], 0)
        self.assertIn("broker_order_cancel_request", events)
        self.assertIn("broker_order_cancel_response", events)

    def test_cancelled_partially_filled_updates_only_filled_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            audit_log = Path(temp_dir) / "audit.jsonl"
            broker = FakeBroker(_snapshot(BrokerOrderStatus.CANCELLED_PART, dealt_quantity=50))
            summary = self._service(broker, audit_log).submit_approval(_approval(), confirm_text="确认实盘")

        self.assertTrue(summary.ok)
        self.assertEqual(summary.filled_quantity, 50)
        self.assertEqual(summary.inventory["trading_qty_sold"], 50)

    def test_fill_after_cancel_creates_reconciliation_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            audit_log = Path(temp_dir) / "audit.jsonl"
            broker = FakeBroker(
                _snapshot(BrokerOrderStatus.SUBMITTED),
                after_cancel=_snapshot(BrokerOrderStatus.CANCELLED_PART, dealt_quantity=50),
            )
            summary = self._service(broker, audit_log).submit_approval(_approval(), confirm_text="确认实盘")
            events = [row["event"] for row in self._events(audit_log)]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.inventory["trading_qty_sold"], 50)
        self.assertIn("reconciliation_warning", events)

    def test_guard_rejection_writes_audit_event_without_broker_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            audit_log = Path(temp_dir) / "audit.jsonl"
            broker = FakeBroker(_snapshot(BrokerOrderStatus.FILLED_ALL, dealt_quantity=100))
            summary = self._service(broker, audit_log).submit_approval(_approval(), confirm_text="确认实盘")
            events = [row["event"] for row in self._events(audit_log)]

        self.assertFalse(summary.ok)
        self.assertIsNone(broker.placed_side)
        self.assertIn("real_order_guard_reject", events)


if __name__ == "__main__":
    unittest.main()
