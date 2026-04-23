"""Execution workflow helpers that turn plans into broker orders."""

from __future__ import annotations

from decimal import Decimal
from time import monotonic as _monotonic
from time import sleep as _sleep

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import TradeBroker
from futu_opend_execution.models import (
    BrokerOrderSnapshot,
    GreyMarketBuyPlan,
    GreyMarketBuyRequest,
    GreyMarketExecutionReport,
    OrderBookSnapshot,
    TimeInForce,
)
from futu_opend_execution.risk import validate_runtime_config
from futu_opend_execution.services.greymarket import build_grey_market_buy_plan


def execute_grey_market_buy(
    request: GreyMarketBuyRequest,
    order_book: OrderBookSnapshot,
    broker: TradeBroker,
    *,
    config: RuntimeConfig | None = None,
    sleep=_sleep,
    monotonic=_monotonic,
) -> GreyMarketExecutionReport:
    runtime_config = config or RuntimeConfig.from_env()
    plan = build_grey_market_buy_plan(request, order_book, config=runtime_config)
    return submit_grey_market_buy_plan(
        plan,
        broker,
        request=request,
        config=runtime_config,
        sleep=sleep,
        monotonic=monotonic,
    )


def submit_grey_market_buy_plan(
    plan: GreyMarketBuyPlan,
    broker: TradeBroker,
    *,
    request: GreyMarketBuyRequest,
    config: RuntimeConfig | None = None,
    sleep=_sleep,
    monotonic=_monotonic,
) -> GreyMarketExecutionReport:
    runtime_config = config or RuntimeConfig.from_env()
    validate_runtime_config(runtime_config)

    submitted_time_in_force = plan.time_in_force
    ioc_emulation_used = False
    cancel_timeout_seconds: float | None = None
    if plan.time_in_force is TimeInForce.IOC and not broker.supports_native_ioc:
        submitted_time_in_force = TimeInForce.DAY
        ioc_emulation_used = True
        timeout_source = (
            request.ioc_timeout_seconds
            if request.ioc_timeout_seconds is not None
            else Decimal(str(runtime_config.default_ioc_timeout_seconds))
        )
        cancel_timeout_seconds = float(timeout_source)

    initial_order = broker.place_limit_buy(
        symbol=plan.symbol,
        quantity=plan.quantity,
        limit_price=plan.selected_limit_price,
        trade_mode=plan.trade_mode,
        time_in_force=submitted_time_in_force,
        remark=request.remark,
    )
    timeline = [initial_order]
    latest_order = initial_order
    canceled_for_timeout = False

    if latest_order.terminal:
        return GreyMarketExecutionReport(
            plan=plan,
            requested_time_in_force=plan.time_in_force,
            submitted_time_in_force=submitted_time_in_force,
            initial_order=initial_order,
            latest_order=latest_order,
            canceled_for_timeout=False,
            ioc_emulation_used=ioc_emulation_used,
            timeline=tuple(timeline),
        )

    started_at = monotonic()
    while not latest_order.terminal:
        elapsed_seconds = monotonic() - started_at
        if cancel_timeout_seconds is not None and elapsed_seconds >= cancel_timeout_seconds:
            latest_order, canceled_for_timeout = _cancel_and_poll_terminal(
                plan=plan,
                initial_order=initial_order,
                broker=broker,
                runtime_config=runtime_config,
                timeline=timeline,
                sleep=sleep,
                monotonic=monotonic,
            )
            break

        sleep_seconds = runtime_config.order_poll_interval_seconds
        if cancel_timeout_seconds is not None:
            sleep_seconds = min(
                sleep_seconds,
                max(cancel_timeout_seconds - elapsed_seconds, 0.0),
            )
        sleep(sleep_seconds)

        if (
            cancel_timeout_seconds is not None
            and monotonic() - started_at >= cancel_timeout_seconds
        ):
            latest_order, canceled_for_timeout = _cancel_and_poll_terminal(
                plan=plan,
                initial_order=initial_order,
                broker=broker,
                runtime_config=runtime_config,
                timeline=timeline,
                sleep=sleep,
                monotonic=monotonic,
            )
            break

        latest_order = broker.get_order(
            order_id=initial_order.order_id,
            symbol=plan.symbol,
            trade_mode=plan.trade_mode,
        )
        timeline.append(latest_order)

    return GreyMarketExecutionReport(
        plan=plan,
        requested_time_in_force=plan.time_in_force,
        submitted_time_in_force=submitted_time_in_force,
        initial_order=initial_order,
        latest_order=latest_order,
        canceled_for_timeout=canceled_for_timeout,
        ioc_emulation_used=ioc_emulation_used,
        timeline=tuple(timeline),
    )


def _poll_after_cancel(
    *,
    broker: TradeBroker,
    order_id: str,
    symbol: str,
    trade_mode,
    poll_interval_seconds: float,
    cancel_grace_seconds: float,
    timeline: list[BrokerOrderSnapshot],
    sleep,
    monotonic,
) -> BrokerOrderSnapshot:
    latest_order = timeline[-1]
    deadline = monotonic() + cancel_grace_seconds

    while monotonic() < deadline:
        sleep(poll_interval_seconds)
        latest_order = broker.get_order(
            order_id=order_id,
            symbol=symbol,
            trade_mode=trade_mode,
        )
        timeline.append(latest_order)
        if latest_order.terminal:
            break

    return latest_order


def _cancel_and_poll_terminal(
    *,
    plan: GreyMarketBuyPlan,
    initial_order: BrokerOrderSnapshot,
    broker: TradeBroker,
    runtime_config: RuntimeConfig,
    timeline: list[BrokerOrderSnapshot],
    sleep,
    monotonic,
) -> tuple[BrokerOrderSnapshot, bool]:
    broker.cancel_order(
        order_id=initial_order.order_id,
        symbol=plan.symbol,
        trade_mode=plan.trade_mode,
    )
    latest_order = _poll_after_cancel(
        broker=broker,
        order_id=initial_order.order_id,
        symbol=plan.symbol,
        trade_mode=plan.trade_mode,
        poll_interval_seconds=runtime_config.order_poll_interval_seconds,
        cancel_grace_seconds=runtime_config.cancel_order_grace_seconds,
        timeline=timeline,
        sleep=sleep,
        monotonic=monotonic,
    )
    return latest_order, True
