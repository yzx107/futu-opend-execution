"""Fail-closed risk guards for real OpenD trading-agent orders."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution.config import RuntimeConfig, is_local_opend_host
from futu_opend_execution.execution.orders import OrderRole, OrderSide, RealOrderIntent
from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.strategy_config import ExecutionMode


@dataclass(slots=True)
class RealOrderGuard:
    """Validate every real order attempt before it reaches OpenD."""

    allow_real_trade: bool = False
    kill_switch_file: Path | None = None
    max_qty: int = 0
    max_notional: Decimal = Decimal("0")
    lot_size: int = 1
    max_order_attempts: int = 1
    safe_window_seconds: float = 30.0
    min_interval_seconds: float = 1.0
    confirmation_phrase: str = "确认实盘"
    experimental_auto_enabled: bool = False
    _client_intent_ids: set[str] = field(default_factory=set)
    _order_times: deque[float] = field(default_factory=deque)

    def validate(
        self,
        intent: RealOrderIntent,
        *,
        execution_mode: ExecutionMode,
        inventory: InventoryState | dict[str, Any] | None,
        market_snapshot: dict[str, Any] | None = None,
        risk_snapshot: dict[str, Any] | None = None,
        runtime_config: RuntimeConfig | None = None,
        confirm_text: str = "",
        enable_auto_cost_reducer: bool = False,
        approved: bool = False,
        now_monotonic: float = 0.0,
        reserve: bool = True,
    ) -> None:
        self._check_real_mode(
            execution_mode,
            confirm_text,
            enable_auto_cost_reducer,
            approved,
            runtime_config,
        )
        self._check_shape(intent)
        self._check_risk_snapshot(risk_snapshot)
        self._check_inventory(intent, inventory)
        self._check_market(intent, market_snapshot)
        if intent.client_intent_id in self._client_intent_ids:
            raise ExecutionValidationError("duplicate client_intent_id blocked")
        self._check_rate_limit(now_monotonic)
        if reserve:
            self._client_intent_ids.add(intent.client_intent_id)
            self._order_times.append(now_monotonic)

    def _check_real_mode(
        self,
        execution_mode: ExecutionMode,
        confirm_text: str,
        enable_auto_cost_reducer: bool,
        approved: bool,
        runtime_config: RuntimeConfig | None,
    ) -> None:
        if execution_mode not in {
            ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
            ExecutionMode.LIVE_REAL_COST_REDUCER_AUTO,
        }:
            raise ExecutionValidationError("real orders require explicit manual real execution mode")
        if not self.allow_real_trade or os.environ.get("FUTU_ALLOW_REAL_TRADE") != "1":
            raise ExecutionValidationError("FUTU_ALLOW_REAL_TRADE=1 is required for real orders")
        config = runtime_config or RuntimeConfig.from_env()
        if not is_local_opend_host(config.futu_host):
            raise ExecutionValidationError("RuntimeConfig futu_host must be local loopback")
        if confirm_text != self.confirmation_phrase:
            raise ExecutionValidationError(f"confirmation phrase required: {self.confirmation_phrase}")
        if self.kill_switch_file is not None and self.kill_switch_file.exists():
            raise ExecutionValidationError("kill switch is active")
        if execution_mode is ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL and not approved:
            raise ExecutionValidationError("manual approval is required")
        if execution_mode is ExecutionMode.LIVE_REAL_COST_REDUCER_AUTO:
            if not self.experimental_auto_enabled:
                raise ExecutionValidationError("experimental auto cost reducer is disabled")
            if not enable_auto_cost_reducer:
                raise ExecutionValidationError("enable_auto_cost_reducer is required")

    def _check_shape(self, intent: RealOrderIntent) -> None:
        order_type = str(getattr(intent, "order_type", "LIMIT")).strip().upper()
        if order_type not in {"LIMIT", "NORMAL"}:
            raise ExecutionValidationError("real orders are limit-only")
        if self.lot_size <= 0:
            raise ExecutionValidationError("lot_size must be positive")
        if intent.quantity <= 0 or intent.quantity % self.lot_size != 0:
            raise ExecutionValidationError("quantity must be positive and lot-aligned")
        if intent.limit_price <= 0:
            raise ExecutionValidationError("limit_price must be positive")
        if self.max_qty <= 0 or self.max_notional <= 0:
            raise ExecutionValidationError("explicit max_qty and max_notional are required")
        if intent.quantity > self.max_qty:
            raise ExecutionValidationError("quantity exceeds max_qty")
        if intent.notional > self.max_notional:
            raise ExecutionValidationError("order notional exceeds max_notional")
        if intent.side is OrderSide.SELL and intent.role is not OrderRole.TRADING_SELL:
            raise ExecutionValidationError("core inventory cannot be sold")
        if intent.side is OrderSide.BUY and intent.role is not OrderRole.TRADING_REBUY:
            raise ExecutionValidationError("buy orders may only rebuy trading inventory")

    def _check_risk_snapshot(self, risk_snapshot: dict[str, Any] | None) -> None:
        if not risk_snapshot:
            return
        if bool(risk_snapshot.get("has_critical")):
            raise ExecutionValidationError("critical risk snapshot blocks real orders")
        if str(risk_snapshot.get("max_severity", "")).strip().upper() == "CRITICAL":
            raise ExecutionValidationError("critical risk snapshot blocks real orders")

    def _check_inventory(
        self,
        intent: RealOrderIntent,
        inventory: InventoryState | dict[str, Any] | None,
    ) -> None:
        if inventory is None:
            raise ExecutionValidationError("inventory snapshot is required")
        available_to_sell = _inventory_int(inventory, "trading_available_to_sell")
        available_to_rebuy = _inventory_int(inventory, "trading_available_to_rebuy")
        if intent.role is OrderRole.TRADING_SELL and intent.quantity > available_to_sell:
            raise ExecutionValidationError("sell quantity exceeds trading inventory")
        if intent.role is OrderRole.TRADING_REBUY and intent.quantity > available_to_rebuy:
            raise ExecutionValidationError("rebuy quantity exceeds sold trading inventory")

    def _check_market(self, intent: RealOrderIntent, market_snapshot: dict[str, Any] | None) -> None:
        if intent.role not in {OrderRole.TRADING_SELL, OrderRole.TRADING_REBUY}:
            return
        if not market_snapshot:
            raise ExecutionValidationError("market snapshot is required")
        if market_snapshot.get("stale"):
            raise ExecutionValidationError("market snapshot is stale")
        spread = _optional_decimal(market_snapshot.get("spread_bps"))
        max_spread = _optional_decimal(market_snapshot.get("max_spread_bps"))
        if spread is None or max_spread is None:
            raise ExecutionValidationError("spread guard requires spread_bps and max_spread_bps")
        if spread > max_spread:
            raise ExecutionValidationError("spread too wide")
        if intent.side is OrderSide.SELL and _optional_decimal(market_snapshot.get("best_bid")) is None:
            raise ExecutionValidationError("best_bid is required")
        if intent.side is OrderSide.BUY and _optional_decimal(market_snapshot.get("best_ask")) is None:
            raise ExecutionValidationError("best_ask is required")

    def _check_rate_limit(self, now_monotonic: float) -> None:
        while self._order_times and now_monotonic - self._order_times[0] >= self.safe_window_seconds:
            self._order_times.popleft()
        if len(self._order_times) >= self.max_order_attempts:
            raise ExecutionValidationError("max order attempts exceeded")
        if self._order_times and now_monotonic - self._order_times[-1] < self.min_interval_seconds:
            raise ExecutionValidationError("order cooldown has not elapsed")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _inventory_int(inventory: InventoryState | dict[str, Any], key: str) -> int:
    if isinstance(inventory, InventoryState):
        return int(getattr(inventory, key))
    value = inventory.get(key)
    if value in {None, ""}:
        raise ExecutionValidationError(f"inventory snapshot missing {key}")
    return int(value)
