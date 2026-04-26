"""Inventory accounting for grey-market cost-reducer dry-run workflows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


class InventoryValidationError(ValueError):
    """Raised when an inventory transition violates hard constraints."""


def _to_decimal(value: Decimal | str | int | float | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(slots=True)
class InventoryState:
    core_qty_target: int
    trading_qty_target: int
    core_qty_filled: int = 0
    trading_qty_filled: int = 0
    trading_qty_sold: int = 0
    trading_qty_rebought: int = 0
    total_buy_notional: Decimal = Decimal("0")
    total_sell_notional: Decimal = Decimal("0")
    estimated_costs: Decimal = Decimal("0")

    @property
    def current_position(self) -> int:
        return self.core_qty_filled + self.trading_qty_filled - self.trading_qty_sold

    @property
    def trading_available_to_sell(self) -> int:
        return max(self.trading_qty_filled - self.trading_qty_sold, 0)

    @property
    def trading_available_to_rebuy(self) -> int:
        return max(self.trading_qty_sold - self.trading_qty_rebought, 0)

    @property
    def economic_net_cost(self) -> Decimal:
        return self.total_buy_notional - self.total_sell_notional + self.estimated_costs

    @property
    def economic_cost_basis(self) -> Decimal:
        if self.current_position <= 0:
            return Decimal("0")
        return self.economic_net_cost / Decimal(self.current_position)

    def seed_opening_inventory(self, *, anchor_price: Decimal | str | int | float) -> None:
        if self.core_qty_filled or self.trading_qty_filled:
            raise InventoryValidationError("Opening inventory is already seeded.")
        anchor = _to_decimal(anchor_price)
        if anchor <= 0:
            raise InventoryValidationError("anchor_price must be positive.")
        self.core_qty_filled = self.core_qty_target
        self.trading_qty_filled = self.trading_qty_target
        self.total_buy_notional += anchor * Decimal(self.current_position)

    def record_trading_sell(
        self,
        *,
        quantity: int,
        price: Decimal | str | int | float,
        estimated_cost: Decimal | str | int | float = Decimal("0"),
    ) -> None:
        if quantity <= 0:
            raise InventoryValidationError("sell quantity must be positive.")
        if quantity > self.trading_available_to_sell:
            raise InventoryValidationError("cannot sell more than trading inventory.")
        self.trading_qty_sold += quantity
        px = _to_decimal(price)
        self.total_sell_notional += px * Decimal(quantity)
        self.estimated_costs += _to_decimal(estimated_cost)

    def record_trading_rebuy(
        self,
        *,
        quantity: int,
        price: Decimal | str | int | float,
        estimated_cost: Decimal | str | int | float = Decimal("0"),
    ) -> None:
        if quantity <= 0:
            raise InventoryValidationError("rebuy quantity must be positive.")
        if quantity > self.trading_available_to_rebuy:
            raise InventoryValidationError(
                "cannot rebuy more than previously sold trading inventory."
            )
        self.trading_qty_rebought += quantity
        self.total_buy_notional += _to_decimal(price) * Decimal(quantity)
        self.estimated_costs += _to_decimal(estimated_cost)


def split_inventory_targets(
    *,
    total_quantity: int,
    lot_size: int,
    core_ratio: Decimal | str | float = Decimal("0.5"),
    trading_ratio: Decimal | str | float = Decimal("0.5"),
) -> InventoryState:
    if total_quantity <= 0:
        raise InventoryValidationError("total_quantity must be positive.")
    if lot_size <= 0:
        raise InventoryValidationError("lot_size must be positive.")

    core = _to_decimal(core_ratio)
    trading = _to_decimal(trading_ratio)
    if core <= 0 or trading <= 0:
        raise InventoryValidationError("core_ratio and trading_ratio must be positive.")
    ratio_sum = core + trading

    total_lots = total_quantity // lot_size
    if total_lots <= 0:
        raise InventoryValidationError("total_quantity must be at least one lot.")

    core_lots = int(
        (Decimal(total_lots) * core / ratio_sum).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    core_lots = max(min(core_lots, total_lots), 0)
    trading_lots = total_lots - core_lots
    if core_lots <= 0 or trading_lots <= 0:
        raise InventoryValidationError("50/50 split requires at least two lots.")

    return InventoryState(
        core_qty_target=core_lots * lot_size,
        trading_qty_target=trading_lots * lot_size,
    )
