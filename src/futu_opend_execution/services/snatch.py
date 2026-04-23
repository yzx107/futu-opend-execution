"""End-to-end grey-market snatch workflow."""

from __future__ import annotations

from decimal import Decimal
from time import monotonic as _monotonic
from time import sleep as _sleep

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import TradeBroker
from futu_opend_execution.execution.market_data import (
    MarketDataClient,
    MarketDataTimeoutError,
)
from futu_opend_execution.models import (
    GreyMarketBuyRequest,
    GreyMarketExecutionReport,
    GreyMarketSnatchReport,
)
from futu_opend_execution.risk import validate_runtime_config
from futu_opend_execution.services.greymarket import build_grey_market_buy_plan
from futu_opend_execution.services.orders import submit_grey_market_buy_plan


def run_grey_market_snatch(
    request: GreyMarketBuyRequest,
    market_data: MarketDataClient,
    *,
    broker: TradeBroker | None = None,
    config: RuntimeConfig | None = None,
    wait_timeout_seconds: float | None = None,
    sleep=_sleep,
    monotonic=_monotonic,
) -> GreyMarketSnatchReport:
    runtime_config = config or RuntimeConfig.from_env()
    validate_runtime_config(runtime_config)

    wait_timeout = (
        runtime_config.default_wait_for_open_timeout_seconds
        if wait_timeout_seconds is None
        else wait_timeout_seconds
    )
    opened_state, waited_seconds = wait_for_grey_market_open(
        request.symbol,
        market_data,
        config=runtime_config,
        wait_timeout_seconds=wait_timeout,
        sleep=sleep,
        monotonic=monotonic,
    )
    market_data.ensure_order_book_subscription(
        symbol=request.symbol,
        depth=runtime_config.default_order_book_depth,
    )
    order_book = market_data.get_order_book_snapshot(
        symbol=request.symbol,
        depth=runtime_config.default_order_book_depth,
    )
    plan = build_grey_market_buy_plan(request, order_book, config=runtime_config)

    execution_report: GreyMarketExecutionReport | None = None
    if broker is not None:
        execution_report = submit_grey_market_buy_plan(
            plan,
            broker,
            request=request,
            config=runtime_config,
            sleep=sleep,
            monotonic=monotonic,
        )

    return GreyMarketSnatchReport(
        market_state=opened_state,
        waited_seconds=Decimal(str(waited_seconds)),
        order_book=order_book,
        plan=plan,
        execution_report=execution_report,
    )


def wait_for_grey_market_open(
    symbol: str,
    market_data: MarketDataClient,
    *,
    config: RuntimeConfig | None = None,
    wait_timeout_seconds: float | None = None,
    sleep=_sleep,
    monotonic=_monotonic,
) -> tuple[str, float]:
    runtime_config = config or RuntimeConfig.from_env()
    validate_runtime_config(runtime_config)

    allowed_states = set(runtime_config.grey_market_open_states)
    wait_timeout = (
        runtime_config.default_wait_for_open_timeout_seconds
        if wait_timeout_seconds is None
        else wait_timeout_seconds
    )

    started_at = monotonic()
    while True:
        state = market_data.get_market_state(symbol=symbol)
        if state in allowed_states:
            return state, monotonic() - started_at

        if monotonic() - started_at >= wait_timeout:
            raise MarketDataTimeoutError(
                f"Timed out waiting for grey-market tradable states {sorted(allowed_states)}; "
                f"last observed state for {symbol} was {state}."
            )

        sleep(runtime_config.quote_poll_interval_seconds)
