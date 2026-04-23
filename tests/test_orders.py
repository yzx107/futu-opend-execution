from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from futu_opend_execution import (
    BrokerConfigurationError,
    BrokerDependencyError,
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    FutuOpenDTradeBroker,
    GreyMarketBuyRequest,
    OrderBookSnapshot,
    QuoteLevel,
    RuntimeConfig,
    TimeInForce,
    TradeMode,
    build_grey_market_buy_plan,
    submit_grey_market_buy_plan,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeTradeBroker:
    supports_native_ioc = False

    def __init__(self, updates: list[BrokerOrderSnapshot]) -> None:
        self._updates = list(updates)
        self.place_calls = 0
        self.cancel_calls = 0
        self.last_time_in_force = None

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
        del symbol, quantity, limit_price, trade_mode, remark

        self.place_calls += 1
        self.last_time_in_force = time_in_force
        return self._updates.pop(0)

    def get_order(
        self,
        *,
        order_id: str,
        symbol: str,
        trade_mode: TradeMode,
    ) -> BrokerOrderSnapshot:
        del order_id, symbol, trade_mode

        if self._updates:
            return self._updates.pop(0)
        raise AssertionError("Unexpected extra get_order call")

    def cancel_order(
        self,
        *,
        order_id: str,
        symbol: str,
        trade_mode: TradeMode,
    ) -> None:
        del order_id, symbol, trade_mode
        self.cancel_calls += 1

    def close(self) -> None:
        return None


class OrderExecutionTests(unittest.TestCase):
    def test_ioc_is_emulated_with_day_plus_cancel(self) -> None:
        request = GreyMarketBuyRequest(
            symbol="09868",
            quantity=250,
            ioc_timeout_seconds="0.5",
        )
        snapshot = OrderBookSnapshot(
            symbol="09868",
            asks=(
                QuoteLevel(price="3.28", quantity=100),
                QuoteLevel(price="3.29", quantity=200),
            ),
        )
        plan = build_grey_market_buy_plan(request, snapshot)
        broker = FakeTradeBroker(
            updates=[
                BrokerOrderSnapshot(
                    order_id="1",
                    symbol="09868",
                    status=BrokerOrderStatus.SUBMITTED,
                    quantity=250,
                    price="3.29",
                ),
                BrokerOrderSnapshot(
                    order_id="1",
                    symbol="09868",
                    status=BrokerOrderStatus.FILLED_PART,
                    quantity=250,
                    price="3.29",
                    dealt_quantity=100,
                    dealt_avg_price="3.28",
                ),
                BrokerOrderSnapshot(
                    order_id="1",
                    symbol="09868",
                    status=BrokerOrderStatus.FILLED_PART,
                    quantity=250,
                    price="3.29",
                    dealt_quantity=100,
                    dealt_avg_price="3.28",
                ),
                BrokerOrderSnapshot(
                    order_id="1",
                    symbol="09868",
                    status=BrokerOrderStatus.CANCELLED_PART,
                    quantity=250,
                    price="3.29",
                    dealt_quantity=100,
                    dealt_avg_price="3.28",
                ),
            ]
        )
        clock = FakeClock()
        config = RuntimeConfig(
            order_poll_interval_seconds=0.2,
            cancel_order_grace_seconds=1.0,
        )

        report = submit_grey_market_buy_plan(
            plan,
            broker,
            request=request,
            config=config,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertTrue(report.ioc_emulation_used)
        self.assertTrue(report.canceled_for_timeout)
        self.assertEqual(report.submitted_time_in_force, TimeInForce.DAY)
        self.assertEqual(report.latest_order.status, BrokerOrderStatus.CANCELLED_PART)
        self.assertEqual(report.latest_order.remaining_quantity, 150)
        self.assertEqual(broker.cancel_calls, 1)
        self.assertEqual(broker.last_time_in_force, TimeInForce.DAY)

    def test_full_fill_finishes_without_cancel(self) -> None:
        request = GreyMarketBuyRequest(symbol="09868", quantity=250)
        snapshot = OrderBookSnapshot(
            symbol="09868",
            asks=(
                QuoteLevel(price="3.28", quantity=100),
                QuoteLevel(price="3.29", quantity=200),
            ),
        )
        plan = build_grey_market_buy_plan(request, snapshot)
        broker = FakeTradeBroker(
            updates=[
                BrokerOrderSnapshot(
                    order_id="1",
                    symbol="09868",
                    status=BrokerOrderStatus.SUBMITTED,
                    quantity=250,
                    price="3.29",
                ),
                BrokerOrderSnapshot(
                    order_id="1",
                    symbol="09868",
                    status=BrokerOrderStatus.FILLED_ALL,
                    quantity=250,
                    price="3.29",
                    dealt_quantity=250,
                    dealt_avg_price="3.286",
                ),
            ]
        )
        clock = FakeClock()
        config = RuntimeConfig(order_poll_interval_seconds=0.2)

        report = submit_grey_market_buy_plan(
            plan,
            broker,
            request=request,
            config=config,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertFalse(report.canceled_for_timeout)
        self.assertTrue(report.fully_filled)
        self.assertEqual(report.latest_order.status, BrokerOrderStatus.FILLED_ALL)
        self.assertEqual(broker.cancel_calls, 0)

    def test_futu_broker_dependency_error_is_explicit(self) -> None:
        with patch(
            "futu_opend_execution.execution.futu_runtime.importlib.import_module",
            side_effect=ImportError("missing futu"),
        ):
            with self.assertRaises(BrokerDependencyError):
                FutuOpenDTradeBroker()

    def test_futu_broker_rejects_native_ioc(self) -> None:
        broker = object.__new__(FutuOpenDTradeBroker)

        with self.assertRaises(BrokerConfigurationError):
            broker._resolve_time_in_force(TimeInForce.IOC)


if __name__ == "__main__":
    unittest.main()
