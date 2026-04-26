"""Fail-closed real-order abstractions for grey-market workflows."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from futu_opend_execution._compat import StrEnum, UTC
from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.strategy_config import ExecutionMode


class GreyOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class GreyOrderRole(StrEnum):
    CORE_BUY = "CORE_BUY"
    TRADING_BUY = "TRADING_BUY"
    TRADING_SELL = "TRADING_SELL"
    TRADING_REBUY = "TRADING_REBUY"


class GreyOrderSource(StrEnum):
    OPEN_TRIGGER = "OPEN_TRIGGER"
    COST_REDUCER = "COST_REDUCER"
    MANUAL_UI = "MANUAL_UI"


@dataclass(frozen=True, slots=True)
class GreyMarketRealOrderIntent:
    symbol: str
    side: GreyOrderSide | str
    quantity: int
    limit_price: Decimal | str | int | float
    role: GreyOrderRole | str
    source: GreyOrderSource | str
    remark: str = "grey_real_order"
    client_intent_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if "." not in symbol:
            symbol = f"HK.{symbol}"
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", self.side if isinstance(self.side, GreyOrderSide) else GreyOrderSide(str(self.side).upper()))
        object.__setattr__(self, "role", self.role if isinstance(self.role, GreyOrderRole) else GreyOrderRole(str(self.role).upper()))
        object.__setattr__(self, "source", self.source if isinstance(self.source, GreyOrderSource) else GreyOrderSource(str(self.source).upper()))
        object.__setattr__(self, "limit_price", _to_decimal(self.limit_price))
        self.validate_shape()

    @property
    def notional(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)

    def validate_shape(self) -> None:
        if self.quantity <= 0:
            raise ExecutionValidationError("quantity must be positive.")
        if self.limit_price <= 0:
            raise ExecutionValidationError("limit_price must be positive.")
        if self.side is GreyOrderSide.SELL and self.role in {
            GreyOrderRole.CORE_BUY,
            GreyOrderRole.TRADING_BUY,
            GreyOrderRole.TRADING_REBUY,
        }:
            raise ExecutionValidationError("SELL orders are allowed only for TRADING_SELL role.")
        if self.side is GreyOrderSide.BUY and self.role is GreyOrderRole.TRADING_SELL:
            raise ExecutionValidationError("TRADING_SELL must use SELL side.")


@dataclass(slots=True)
class RealOrderGuard:
    runtime_config: RuntimeConfig
    kill_switch_file: Path | None = None
    max_qty: int = 0
    max_notional: Decimal = Decimal("0")
    lot_size: int = 1
    max_order_attempts: int = 3
    safe_window_seconds: float = 30.0
    min_interval_seconds: float = 0.05
    confirmation_phrase: str = "确认实盘"
    experimental_auto_enabled: bool = False
    _client_intent_ids: set[str] = field(default_factory=set)
    _order_times: deque[float] = field(default_factory=deque)

    def validate(
        self,
        intent: GreyMarketRealOrderIntent,
        *,
        execution_mode: ExecutionMode,
        inventory: InventoryState | None = None,
        market_snapshot: dict[str, Any] | None = None,
        confirm_text: str = "",
        enable_auto_cost_reducer: bool = False,
        approved: bool = False,
        now_monotonic: float = 0.0,
        reserve: bool = True,
    ) -> None:
        if execution_mode not in {
            ExecutionMode.LIVE_REAL_BUY_ONLY,
            ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
            ExecutionMode.LIVE_REAL_COST_REDUCER_AUTO,
        }:
            raise ExecutionValidationError("real orders require an explicit real execution mode.")
        if not self.runtime_config.allow_real_trade:
            raise ExecutionValidationError("FUTU_ALLOW_REAL_TRADE=1 is required for real orders.")
        if confirm_text != self.confirmation_phrase:
            raise ExecutionValidationError(f"real orders require confirmation phrase: {self.confirmation_phrase}")
        if self.kill_switch_file is not None and self.kill_switch_file.exists():
            raise ExecutionValidationError("kill switch is active; real order blocked.")
        if self.lot_size <= 0:
            raise ExecutionValidationError("lot_size must be positive.")
        if intent.quantity % self.lot_size != 0:
            raise ExecutionValidationError("quantity must be lot-aligned.")
        if self.max_qty > 0 and intent.quantity > self.max_qty:
            raise ExecutionValidationError("quantity exceeds max_qty.")
        if self.max_notional > 0 and intent.notional > self.max_notional:
            raise ExecutionValidationError("order notional exceeds max_notional.")
        if intent.client_intent_id in self._client_intent_ids:
            raise ExecutionValidationError("duplicate client_intent_id blocked.")

        self._check_mode_specific_gates(
            intent,
            execution_mode=execution_mode,
            approved=approved,
            enable_auto_cost_reducer=enable_auto_cost_reducer,
        )
        self._check_inventory_constraints(intent, inventory)
        self._check_market_constraints(intent, market_snapshot)
        self._check_rate_limit(now_monotonic)

        if reserve:
            self._client_intent_ids.add(intent.client_intent_id)
            self._order_times.append(now_monotonic)

    def _check_mode_specific_gates(
        self,
        intent: GreyMarketRealOrderIntent,
        *,
        execution_mode: ExecutionMode,
        approved: bool,
        enable_auto_cost_reducer: bool,
    ) -> None:
        is_cost_reducer = intent.source is GreyOrderSource.COST_REDUCER
        if execution_mode is ExecutionMode.LIVE_REAL_BUY_ONLY:
            if intent.side is GreyOrderSide.SELL or is_cost_reducer:
                raise ExecutionValidationError("buy-only mode blocks cost reducer sell/rebuy orders.")
            return

        if execution_mode is ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL:
            if not approved:
                raise ExecutionValidationError("manual approval is required before real cost reducer execution.")
            return

        if execution_mode is ExecutionMode.LIVE_REAL_COST_REDUCER_AUTO:
            if not self.experimental_auto_enabled:
                raise ExecutionValidationError("experimental auto cost reducer is disabled by config.")
            if not enable_auto_cost_reducer:
                raise ExecutionValidationError("enable_auto_cost_reducer checkbox is required.")

    def _check_inventory_constraints(
        self,
        intent: GreyMarketRealOrderIntent,
        inventory: InventoryState | None,
    ) -> None:
        if intent.side is GreyOrderSide.SELL:
            if intent.role is not GreyOrderRole.TRADING_SELL:
                raise ExecutionValidationError("core inventory cannot be sold by cost reducer.")
            if inventory is None:
                raise ExecutionValidationError("inventory state is required for real sell.")
            if intent.quantity > inventory.trading_available_to_sell:
                raise ExecutionValidationError("sell quantity exceeds trading inventory.")
        if intent.role is GreyOrderRole.TRADING_REBUY:
            if inventory is None:
                raise ExecutionValidationError("inventory state is required for real rebuy.")
            if intent.quantity > inventory.trading_available_to_rebuy:
                raise ExecutionValidationError("rebuy quantity exceeds sold trading inventory.")

    def _check_market_constraints(
        self,
        intent: GreyMarketRealOrderIntent,
        market_snapshot: dict[str, Any] | None,
    ) -> None:
        if intent.source is not GreyOrderSource.COST_REDUCER:
            return
        if not market_snapshot:
            raise ExecutionValidationError("market snapshot is required for cost reducer real orders.")
        if market_snapshot.get("stale"):
            raise ExecutionValidationError("market snapshot is stale.")
        quote_age = _optional_decimal(market_snapshot.get("quote_age_seconds"))
        max_quote_age = _optional_decimal(market_snapshot.get("max_quote_age_seconds")) or Decimal("5")
        if quote_age is not None and quote_age > max_quote_age:
            raise ExecutionValidationError("market snapshot is stale.")
        spread_bps = _optional_decimal(market_snapshot.get("spread_bps"))
        max_spread_bps = _optional_decimal(market_snapshot.get("max_spread_bps"))
        if spread_bps is None or max_spread_bps is None:
            raise ExecutionValidationError("spread guard requires spread_bps and max_spread_bps.")
        if spread_bps > max_spread_bps:
            raise ExecutionValidationError("spread too wide for real cost reducer order.")
        if intent.side is GreyOrderSide.SELL and _optional_decimal(market_snapshot.get("best_bid")) is None:
            raise ExecutionValidationError("best_bid is required for real sell.")
        if intent.side is GreyOrderSide.BUY and _optional_decimal(market_snapshot.get("best_ask")) is None:
            raise ExecutionValidationError("best_ask is required for real buy.")

    def _check_rate_limit(self, now_monotonic: float) -> None:
        while self._order_times and now_monotonic - self._order_times[0] >= self.safe_window_seconds:
            self._order_times.popleft()
        if len(self._order_times) >= self.max_order_attempts:
            raise ExecutionValidationError("max order attempts exceeded.")
        if self._order_times and now_monotonic - self._order_times[-1] < self.min_interval_seconds:
            raise ExecutionValidationError("order cooldown has not elapsed.")


def _to_decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return _to_decimal(value)
