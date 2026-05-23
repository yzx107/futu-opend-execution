"""Shared validation and safety checks."""

from __future__ import annotations

from futu_opend_execution.config import RuntimeConfig, is_local_opend_host
from futu_opend_execution.models import OrderBookSnapshot


class ExecutionValidationError(ValueError):
    """Base class for execution validation failures."""


class QuoteValidationError(ExecutionValidationError):
    """Raised when a quote/order-book snapshot is malformed."""


class RealTradeDisabledError(ExecutionValidationError):
    """Raised when real trading gates are not satisfied."""


def validate_runtime_config(config: RuntimeConfig) -> None:
    if not config.futu_host.strip():
        raise ExecutionValidationError("FUTU_HOST must not be empty")
    if not is_local_opend_host(config.futu_host):
        raise ExecutionValidationError("FUTU_HOST must be local loopback so OpenD traffic stays on this machine")
    if config.futu_port <= 0:
        raise ExecutionValidationError("FUTU_PORT must be positive")
    if config.order_poll_interval_seconds <= 0:
        raise ExecutionValidationError("FUTU_ORDER_POLL_INTERVAL_SECONDS must be positive")
    if config.cancel_order_grace_seconds <= 0:
        raise ExecutionValidationError("FUTU_CANCEL_ORDER_GRACE_SECONDS must be positive")
    if config.default_order_book_depth <= 0:
        raise ExecutionValidationError("FUTU_DEFAULT_ORDER_BOOK_DEPTH must be positive")


def validate_order_book(snapshot: OrderBookSnapshot) -> None:
    if not snapshot.symbol:
        raise QuoteValidationError("symbol is required")
    if not snapshot.asks:
        raise QuoteValidationError("at least one ask level is required")
    previous = None
    for level in snapshot.asks:
        if level.price <= 0 or level.quantity <= 0:
            raise QuoteValidationError("ask levels must have positive price and quantity")
        if previous is not None and level.price < previous:
            raise QuoteValidationError("ask levels must be sorted low to high")
        previous = level.price
