"""Fill-ledger and inventory reconciliation helpers for real trading-agent orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from futu_opend_execution.inventory import InventoryState, InventoryValidationError
from futu_opend_execution.execution.orders import OrderRole, OrderSide, RealOrderIntent


def _to_decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class FillRecord:
    order_id: str
    side: OrderSide | str
    role: OrderRole | str
    quantity: int
    price: Decimal | str | int | float
    deal_id: str | None = None
    updated_time: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", self.side if isinstance(self.side, OrderSide) else OrderSide(str(self.side).upper()))
        object.__setattr__(self, "role", self.role if isinstance(self.role, OrderRole) else OrderRole(str(self.role).upper()))
        object.__setattr__(self, "price", _to_decimal(self.price))
        if self.quantity <= 0:
            raise InventoryValidationError("fill quantity must be positive.")

    @property
    def idempotency_key(self) -> str:
        if self.deal_id:
            return f"deal:{self.deal_id}"
        return "|".join(
            [
                "best_effort",
                self.order_id,
                self.side.value,
                self.role.value,
                str(self.quantity),
                str(self.price),
                str(self.updated_time or ""),
            ]
        )


@dataclass(slots=True)
class FillLedger:
    applied_keys: set[str] = field(default_factory=set)
    fills: list[FillRecord] = field(default_factory=list)

    def add(self, fill: FillRecord) -> bool:
        key = fill.idempotency_key
        if key in self.applied_keys:
            return False
        self.applied_keys.add(key)
        self.fills.append(fill)
        return True


@dataclass(slots=True)
class InventoryManager:
    inventory: InventoryState
    ledger: FillLedger = field(default_factory=FillLedger)
    intended_orders: dict[str, RealOrderIntent] = field(default_factory=dict)
    cancelled_orders: set[str] = field(default_factory=set)

    def record_intent(self, intent: RealOrderIntent) -> dict[str, Any]:
        self.intended_orders[intent.client_intent_id] = intent
        return {"event": "real_order_intent", "client_intent_id": intent.client_intent_id}

    def mark_cancelled(self, order_id: str) -> dict[str, Any]:
        self.cancelled_orders.add(str(order_id))
        return {"event": "real_order_cancel_response", "order_id": str(order_id), "cancelled": True}

    def apply_fill(self, fill: FillRecord) -> dict[str, Any]:
        if fill.order_id in self.cancelled_orders:
            return {
                "event": "inventory_reconciliation_warning",
                "order_id": fill.order_id,
                "message": "fill for cancelled order ignored pending manual review",
            }
        if not self.ledger.add(fill):
            return {
                "event": "real_fill_duplicate_ignored",
                "order_id": fill.order_id,
                "deal_id": fill.deal_id,
            }

        if fill.role is OrderRole.CORE_BUY:
            self.inventory.core_qty_filled += fill.quantity
            self.inventory.total_buy_notional += fill.price * Decimal(fill.quantity)
        elif fill.role is OrderRole.TRADING_BUY:
            self.inventory.trading_qty_filled += fill.quantity
            self.inventory.total_buy_notional += fill.price * Decimal(fill.quantity)
        elif fill.role is OrderRole.TRADING_SELL:
            self.inventory.record_trading_sell(quantity=fill.quantity, price=fill.price)
        elif fill.role is OrderRole.TRADING_REBUY:
            self.inventory.record_trading_rebuy(quantity=fill.quantity, price=fill.price)
        return {
            "event": "real_fill_applied",
            "order_id": fill.order_id,
            "deal_id": fill.deal_id,
            "quantity": fill.quantity,
            "price": str(fill.price),
            "inventory": self.snapshot(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "core_qty_target": self.inventory.core_qty_target,
            "trading_qty_target": self.inventory.trading_qty_target,
            "core_qty_filled": self.inventory.core_qty_filled,
            "trading_qty_filled": self.inventory.trading_qty_filled,
            "trading_qty_sold": self.inventory.trading_qty_sold,
            "trading_qty_rebought": self.inventory.trading_qty_rebought,
            "current_position": self.inventory.current_position,
            "trading_available_to_sell": self.inventory.trading_available_to_sell,
            "trading_available_to_rebuy": self.inventory.trading_available_to_rebuy,
            "economic_cost_basis": str(self.inventory.economic_cost_basis),
            "economic_net_cost": str(self.inventory.economic_net_cost),
        }


class PositionReconciler:
    def reconcile(self, manager: InventoryManager) -> dict[str, Any]:
        return {
            "event": "inventory_reconciled",
            "inventory": manager.snapshot(),
            "fill_count": len(manager.ledger.fills),
            "intended_order_count": len(manager.intended_orders),
        }
