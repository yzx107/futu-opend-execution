"""Broker interfaces and broker-specific exceptions."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from futu_opend_execution.models import BrokerOrderSnapshot, TimeInForce, TradeMode


class BrokerError(RuntimeError):
    """Base class for broker-related failures."""


class BrokerDependencyError(BrokerError):
    """Raised when an optional broker SDK is unavailable."""


class BrokerConfigurationError(BrokerError):
    """Raised when runtime configuration cannot be mapped to the broker."""


class BrokerResponseError(BrokerError):
    """Raised when the broker returns an error or malformed payload."""


class BrokerOrderNotFoundError(BrokerError):
    """Raised when a broker order cannot be queried back."""


class TradeBroker(Protocol):
    """Minimal broker interface required by the execution service."""

    supports_native_ioc: bool

    def place_limit_buy(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: Decimal,
        trade_mode: TradeMode,
        time_in_force: TimeInForce,
        remark: str | None = None,
    ) -> BrokerOrderSnapshot:
        """Submit a limit buy order and return the first observed broker snapshot."""

    def place_limit_sell(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: Decimal,
        trade_mode: TradeMode,
        time_in_force: TimeInForce,
        remark: str | None = None,
    ) -> BrokerOrderSnapshot:
        """Submit a limit sell order and return the first observed broker snapshot."""

    def get_order(
        self,
        *,
        order_id: str,
        symbol: str,
        trade_mode: TradeMode,
    ) -> BrokerOrderSnapshot:
        """Query the latest known state for a broker order."""

    def cancel_order(
        self,
        *,
        order_id: str,
        symbol: str,
        trade_mode: TradeMode,
    ) -> None:
        """Cancel the remaining quantity of an existing order."""

    def close(self) -> None:
        """Release any broker resources held by the adapter."""
