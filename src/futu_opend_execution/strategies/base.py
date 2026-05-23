"""Minimal strategy protocol."""

from __future__ import annotations

from typing import Protocol

from futu_opend_execution.data.market import MarketState
from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.services.cost_reducer import CostReducerState, CostReducerExecutableIntent


class Strategy(Protocol):
    def evaluate(
        self,
        *,
        market: MarketState,
        inventory: InventoryState,
        state: CostReducerState,
    ) -> CostReducerExecutableIntent:
        """Return one executable or blocked intent."""
