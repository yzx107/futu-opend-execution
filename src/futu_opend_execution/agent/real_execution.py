"""Guarded manual real-order execution service."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution._compat import UTC
from futu_opend_execution.agent.approval import (
    PendingRealOrderApproval,
    approval_to_intent,
    load_approval_file,
    validate_approval,
)
from futu_opend_execution.agent.risk import RealOrderGuard
from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import TradeBroker
from futu_opend_execution.execution.futu import FutuOpenDTradeBroker
from futu_opend_execution.execution.orders import OrderRole, OrderSide, RealOrderIntent
from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.models import BrokerOrderSnapshot, TimeInForce, TradeMode
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.services.reconciliation import (
    FillRecord,
    InventoryManager,
)
from futu_opend_execution.strategy_config import ExecutionMode


@dataclass(frozen=True, slots=True)
class RealExecutionSummary:
    ok: bool
    approval_id: str
    order_id: str | None
    status: str
    filled_quantity: int
    remaining_quantity: int
    cancelled: bool
    inventory: dict[str, Any]
    error: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "approval_id": self.approval_id,
            "order_id": self.order_id,
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "cancelled": self.cancelled,
            "inventory": self.inventory,
            "error": self.error,
        }


class RealExecutionService:
    """Submit only approval-backed limit orders after `RealOrderGuard` passes."""

    def __init__(
        self,
        *,
        broker: TradeBroker | None = None,
        guard: RealOrderGuard,
        audit_log_path: str | Path,
        runtime_config: RuntimeConfig | None = None,
        inventory_manager: InventoryManager | None = None,
        poll_interval_seconds: float = 0.2,
        timeout_seconds: float = 1.0,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self._runtime_config = runtime_config or RuntimeConfig.from_env()
        self._broker = broker
        self._owns_broker = broker is None
        self._guard = guard
        self._audit_log_path = Path(audit_log_path)
        self._inventory_manager = inventory_manager
        self._poll_interval_seconds = poll_interval_seconds
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._applied_dealt_qty: dict[str, int] = {}

    def close(self) -> None:
        if self._owns_broker and self._broker is not None:
            self._broker.close()

    def submit_approval_file(
        self,
        approval_file: str | Path,
        *,
        confirm_text: str,
    ) -> RealExecutionSummary:
        approval = load_approval_file(approval_file)
        return self.submit_approval(approval, confirm_text=confirm_text)

    def submit_approval(
        self,
        approval: PendingRealOrderApproval,
        *,
        confirm_text: str,
    ) -> RealExecutionSummary:
        intent = approval_to_intent(approval)
        manager = self._inventory_manager or _manager_from_approval(approval)
        self._inventory_manager = manager
        manager.record_intent(intent)
        self._audit("real_order_intent", _intent_to_audit(intent, approval))

        try:
            validate_approval(
                approval,
                confirmation_phrase=self._guard.confirmation_phrase,
                kill_switch_file=self._guard.kill_switch_file,
                require_approved=True,
            )
            self._guard.validate(
                intent,
                execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
                inventory=approval.inventory_snapshot,
                market_snapshot=approval.market_snapshot,
                risk_snapshot=approval.risk_snapshot,
                runtime_config=self._runtime_config,
                confirm_text=confirm_text,
                approved=approval.approved,
                now_monotonic=self._monotonic(),
            )
        except ExecutionValidationError as exc:
            return self._guard_reject(approval, manager, str(exc))

        broker = self._broker or FutuOpenDTradeBroker(self._runtime_config)
        self._broker = broker

        order = self._place_limit_order(broker, intent)
        self._audit("broker_order_response", _snapshot_to_audit(order))
        self._apply_new_fill(order, intent, manager)

        final_order = self._poll_until_terminal_or_timeout(broker, intent, order, manager)
        summary = RealExecutionSummary(
            ok=final_order.fully_filled or final_order.dealt_quantity > 0,
            approval_id=approval.approval_id,
            order_id=final_order.order_id,
            status=final_order.status.value,
            filled_quantity=final_order.dealt_quantity,
            remaining_quantity=final_order.remaining_quantity,
            cancelled=final_order.status.value.startswith("CANCELLED"),
            inventory=manager.snapshot(),
        )
        self._audit("real_order_summary", summary.to_jsonable())
        return summary

    def _guard_reject(
        self,
        approval: PendingRealOrderApproval,
        manager: InventoryManager,
        reason: str,
    ) -> RealExecutionSummary:
        payload = {"approval_id": approval.approval_id, "reason": reason}
        self._audit("real_order_guard_reject", payload)
        summary = RealExecutionSummary(
            ok=False,
            approval_id=approval.approval_id,
            order_id=None,
            status="REJECTED",
            filled_quantity=0,
            remaining_quantity=approval.quantity,
            cancelled=False,
            inventory=manager.snapshot(),
            error=reason,
        )
        self._audit("real_order_summary", summary.to_jsonable())
        return summary

    def _place_limit_order(
        self,
        broker: TradeBroker,
        intent: RealOrderIntent,
    ) -> BrokerOrderSnapshot:
        kwargs = {
            "symbol": intent.symbol,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "trade_mode": TradeMode.REAL,
            "time_in_force": TimeInForce.DAY,
            "remark": intent.remark,
        }
        if intent.side is OrderSide.SELL:
            return broker.place_limit_sell(**kwargs)
        return broker.place_limit_buy(**kwargs)

    def _poll_until_terminal_or_timeout(
        self,
        broker: TradeBroker,
        intent: RealOrderIntent,
        order: BrokerOrderSnapshot,
        manager: InventoryManager,
    ) -> BrokerOrderSnapshot:
        deadline = self._monotonic() + self._timeout_seconds
        latest = order

        while not latest.terminal and self._monotonic() < deadline:
            if self._poll_interval_seconds > 0:
                self._sleep(self._poll_interval_seconds)
            latest = broker.get_order(
                order_id=order.order_id,
                symbol=intent.symbol,
                trade_mode=TradeMode.REAL,
            )
            self._audit("broker_order_status", _snapshot_to_audit(latest))
            self._apply_new_fill(latest, intent, manager)

        if latest.terminal:
            return latest

        self._audit(
            "broker_order_cancel_request",
            {"order_id": order.order_id, "symbol": intent.symbol},
        )
        broker.cancel_order(order_id=order.order_id, symbol=intent.symbol, trade_mode=TradeMode.REAL)
        manager.mark_cancelled(order.order_id)
        self._audit(
            "broker_order_cancel_response",
            {"order_id": order.order_id, "symbol": intent.symbol, "ok": True},
        )

        latest = broker.get_order(
            order_id=order.order_id,
            symbol=intent.symbol,
            trade_mode=TradeMode.REAL,
        )
        self._audit("broker_order_status", _snapshot_to_audit(latest))
        self._apply_new_fill(latest, intent, manager)
        return latest

    def _apply_new_fill(
        self,
        order: BrokerOrderSnapshot,
        intent: RealOrderIntent,
        manager: InventoryManager,
    ) -> None:
        previous = self._applied_dealt_qty.get(order.order_id, 0)
        delta = max(order.dealt_quantity - previous, 0)
        if delta <= 0:
            return
        self._applied_dealt_qty[order.order_id] = order.dealt_quantity
        fill = FillRecord(
            order_id=order.order_id,
            side=intent.side,
            role=intent.role,
            quantity=delta,
            price=order.dealt_avg_price or order.price,
            updated_time=order.updated_time,
        )
        result = manager.apply_fill(fill)
        if result["event"] == "reconciliation_warning":
            self._audit("reconciliation_warning", _jsonable(result))
        if result["event"] != "real_fill_duplicate_ignored":
            self._audit("broker_fill", _fill_to_audit(fill, result))

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"event": event, "ts": datetime.now(UTC).isoformat(), **_jsonable(payload)}
        with self._audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _manager_from_approval(approval: PendingRealOrderApproval) -> InventoryManager:
    snapshot = approval.inventory_snapshot
    sellable = int(snapshot.get("trading_available_to_sell") or 0)
    rebuyable = int(snapshot.get("trading_available_to_rebuy") or 0)
    trading_filled = int(snapshot.get("trading_qty_filled") or (sellable + rebuyable))
    trading_sold = int(snapshot.get("trading_qty_sold") or rebuyable)
    inventory = InventoryState(
        core_qty_target=int(snapshot.get("core_qty_target") or 0),
        trading_qty_target=int(snapshot.get("trading_qty_target") or trading_filled),
        core_qty_filled=int(snapshot.get("core_qty_filled") or 0),
        trading_qty_filled=trading_filled,
        trading_qty_sold=trading_sold,
        trading_qty_rebought=int(snapshot.get("trading_qty_rebought") or 0),
        total_buy_notional=Decimal(str(snapshot.get("total_buy_notional") or "0")),
        total_sell_notional=Decimal(str(snapshot.get("total_sell_notional") or "0")),
        estimated_costs=Decimal(str(snapshot.get("estimated_costs") or "0")),
    )
    return InventoryManager(inventory)


def _intent_to_audit(
    intent: RealOrderIntent,
    approval: PendingRealOrderApproval,
) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "signal_id": approval.signal_id,
        "client_intent_id": intent.client_intent_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "role": intent.role.value,
        "quantity": intent.quantity,
        "limit_price": str(intent.limit_price),
        "expected_edge_bps": str(approval.expected_edge_bps),
    }


def _snapshot_to_audit(order: BrokerOrderSnapshot) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "status": order.status.value,
        "quantity": order.quantity,
        "price": str(order.price),
        "dealt_quantity": order.dealt_quantity,
        "dealt_avg_price": None if order.dealt_avg_price is None else str(order.dealt_avg_price),
        "remaining_quantity": order.remaining_quantity,
        "updated_time": order.updated_time,
        "message": order.message,
    }


def _fill_to_audit(fill: FillRecord, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": fill.order_id,
        "deal_id": fill.deal_id,
        "side": fill.side.value,
        "role": fill.role.value,
        "quantity": fill.quantity,
        "price": str(fill.price),
        "updated_time": fill.updated_time,
        "inventory": result.get("inventory", {}),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value
