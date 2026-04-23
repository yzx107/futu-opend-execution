"""Grey-market order planning logic."""

from __future__ import annotations

from decimal import Decimal

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.simulator import simulate_limit_buy
from futu_opend_execution.models import (
    GreyMarketBuyPlan,
    GreyMarketBuyRequest,
    MarketSession,
    OrderBookSnapshot,
    OrderSide,
    QuoteLevel,
    TradeMode,
)
from futu_opend_execution.risk import (
    InsufficientLiquidityError,
    PriceCapExceededError,
    QuoteValidationError,
    validate_buy_request,
    validate_order_book,
)


def _required_minimum_limit_price(
    *,
    asks: tuple[QuoteLevel, ...],
    target_quantity: int,
) -> tuple[Decimal, int]:
    cumulative_quantity = 0
    marginal_price = None

    for level in asks:
        cumulative_quantity += level.quantity
        marginal_price = level.price
        if cumulative_quantity >= target_quantity:
            return marginal_price, cumulative_quantity

    if marginal_price is None:
        raise QuoteValidationError("At least one ask level is required.")

    return marginal_price, cumulative_quantity


def build_grey_market_buy_plan(
    request: GreyMarketBuyRequest,
    order_book: OrderBookSnapshot,
    *,
    config: RuntimeConfig | None = None,
) -> GreyMarketBuyPlan:
    runtime_config = config or RuntimeConfig.from_env()

    validate_buy_request(request, runtime_config)
    validate_order_book(order_book)

    if order_book.symbol != request.symbol:
        raise QuoteValidationError(
            f"Order symbol {request.symbol} does not match snapshot {order_book.symbol}."
        )
    if order_book.session is not MarketSession.GREY:
        raise QuoteValidationError("Grey-market planner only accepts GREY session quotes.")

    minimum_limit_price, visible_quantity = _required_minimum_limit_price(
        asks=order_book.asks,
        target_quantity=request.quantity,
    )

    if visible_quantity < request.quantity and not request.allow_partial_fill:
        raise InsufficientLiquidityError(
            "Visible ask quantity is below the target quantity. "
            "Refusing to submit a buy order that is unlikely to complete."
        )

    selected_limit_price = minimum_limit_price + (
        request.tick_size * request.price_buffer_ticks
    )
    if (
        request.max_limit_price is not None
        and selected_limit_price > request.max_limit_price
    ):
        raise PriceCapExceededError(
            "Required fill price exceeds the configured max limit price."
        )

    expected_fill = simulate_limit_buy(
        asks=order_book.asks,
        quantity=request.quantity,
        limit_price=selected_limit_price,
    )
    if not request.allow_partial_fill and not expected_fill.fully_filled:
        raise InsufficientLiquidityError(
            "Visible order book still cannot fully satisfy the order at the selected limit."
        )

    notes = [
        "Selected limit price is the lowest visible ask price needed to cover the target quantity.",
        "Default time-in-force is IOC so any unfilled remainder is cancelled instead of resting.",
        "Visible book liquidity can disappear before submission; this plan is a best-effort snapshot calculation.",
    ]
    if request.price_buffer_ticks > 0:
        notes.append(
            "A positive price buffer was added above the minimum visible fill price to improve queue priority."
        )
    if request.allow_partial_fill:
        notes.append("Partial fill mode is enabled.")
    if request.trade_mode is TradeMode.REAL:
        notes.append("Real trade mode passed the environment gate.")

    return GreyMarketBuyPlan(
        symbol=request.symbol,
        quantity=request.quantity,
        side=OrderSide.BUY,
        trade_mode=request.trade_mode,
        session=order_book.session,
        time_in_force=request.time_in_force,
        minimum_limit_price=minimum_limit_price,
        selected_limit_price=selected_limit_price,
        expected_fill=expected_fill,
        notes=tuple(notes),
    )
