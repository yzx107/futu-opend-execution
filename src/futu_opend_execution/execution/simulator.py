"""Simulation helpers for limit-order execution against a visible order book."""

from __future__ import annotations

from decimal import Decimal

from futu_opend_execution.models import FillLeg, QuoteLevel, SimulatedExecutionResult


def simulate_limit_buy(
    *,
    asks: tuple[QuoteLevel, ...],
    quantity: int,
    limit_price: Decimal,
) -> SimulatedExecutionResult:
    remaining_quantity = quantity
    total_notional = Decimal("0")
    fills: list[FillLeg] = []

    for level in asks:
        if remaining_quantity <= 0:
            break
        if level.price > limit_price:
            break

        filled_here = min(remaining_quantity, level.quantity)
        fills.append(FillLeg(price=level.price, quantity=filled_here))
        total_notional += level.price * filled_here
        remaining_quantity -= filled_here

    filled_quantity = quantity - remaining_quantity
    average_price = None
    if filled_quantity > 0:
        average_price = total_notional / Decimal(filled_quantity)

    return SimulatedExecutionResult(
        requested_quantity=quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=remaining_quantity,
        average_price=average_price,
        total_notional=total_notional,
        fills=tuple(fills),
    )
