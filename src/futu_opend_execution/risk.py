"""Validation and safety checks for execution planning."""

from __future__ import annotations

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.models import GreyMarketBuyRequest, OrderBookSnapshot, TradeMode


class ExecutionValidationError(ValueError):
    """Base class for execution input validation failures."""


class QuoteValidationError(ExecutionValidationError):
    """Raised when an order book snapshot is malformed."""


class InsufficientLiquidityError(ExecutionValidationError):
    """Raised when the visible ask book cannot satisfy the desired quantity."""


class PriceCapExceededError(ExecutionValidationError):
    """Raised when the minimum fill price exceeds the user's price cap."""


class RealTradeDisabledError(ExecutionValidationError):
    """Raised when a real order is attempted without the explicit safety gate."""


def validate_runtime_config(config: RuntimeConfig) -> None:
    if not config.futu_host.strip():
        raise ExecutionValidationError("FUTU_HOST must not be empty.")
    if config.futu_port <= 0:
        raise ExecutionValidationError("FUTU_PORT must be a positive integer.")
    if config.order_poll_interval_seconds <= 0:
        raise ExecutionValidationError(
            "FUTU_ORDER_POLL_INTERVAL_SECONDS must be positive."
        )
    if config.cancel_order_grace_seconds <= 0:
        raise ExecutionValidationError(
            "FUTU_CANCEL_ORDER_GRACE_SECONDS must be positive."
        )
    if config.default_ioc_timeout_seconds <= 0:
        raise ExecutionValidationError(
            "FUTU_DEFAULT_IOC_TIMEOUT_SECONDS must be positive."
        )
    if config.quote_poll_interval_seconds <= 0:
        raise ExecutionValidationError(
            "FUTU_QUOTE_POLL_INTERVAL_SECONDS must be positive."
        )
    if config.default_wait_for_open_timeout_seconds <= 0:
        raise ExecutionValidationError(
            "FUTU_DEFAULT_WAIT_FOR_OPEN_TIMEOUT_SECONDS must be positive."
        )
    if config.default_order_book_depth <= 0:
        raise ExecutionValidationError(
            "FUTU_DEFAULT_ORDER_BOOK_DEPTH must be positive."
        )


def validate_order_book(snapshot: OrderBookSnapshot) -> None:
    if not snapshot.symbol:
        raise QuoteValidationError("Snapshot symbol must not be empty.")
    if not snapshot.asks:
        raise QuoteValidationError("At least one ask level is required.")

    last_price = None
    for level in snapshot.asks:
        if level.price <= 0:
            raise QuoteValidationError("Ask prices must be positive.")
        if level.quantity <= 0:
            raise QuoteValidationError("Ask quantities must be positive.")
        if last_price is not None and level.price < last_price:
            raise QuoteValidationError("Ask levels must be sorted from low to high.")
        last_price = level.price


def validate_buy_request(request: GreyMarketBuyRequest, config: RuntimeConfig) -> None:
    validate_runtime_config(config)

    if not request.symbol:
        raise ExecutionValidationError("Order symbol must not be empty.")
    if request.quantity <= 0:
        raise ExecutionValidationError("Order quantity must be positive.")
    if request.tick_size <= 0:
        raise ExecutionValidationError("Tick size must be positive.")
    if request.price_buffer_ticks < 0:
        raise ExecutionValidationError("Price buffer ticks must be zero or greater.")
    if request.max_limit_price is not None and request.max_limit_price <= 0:
        raise ExecutionValidationError("Max limit price must be positive when provided.")
    if request.ioc_timeout_seconds is not None and request.ioc_timeout_seconds <= 0:
        raise ExecutionValidationError(
            "IOC timeout seconds must be positive when provided."
        )
    if request.trade_mode is TradeMode.REAL and not config.allow_real_trade:
        raise RealTradeDisabledError(
            "Real trading is disabled. Set FUTU_ALLOW_REAL_TRADE=1 only after "
            "adding separate operational safeguards."
        )
