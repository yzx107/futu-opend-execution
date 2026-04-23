from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from futu_opend_execution import (
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    GreyMarketBuyRequest,
    MarketDataTimeoutError,
    OrderBookSnapshot,
    QuoteLevel,
    RuntimeConfig,
    TimeInForce,
    TradeMode,
    run_grey_market_snatch,
    wait_for_grey_market_open,
)
from futu_opend_execution.execution.futu_runtime import load_futu_module


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeMarketDataClient:
    def __init__(self, states: list[str], order_book: OrderBookSnapshot) -> None:
        self._states = list(states)
        self._last_state = states[-1]
        self._order_book = order_book
        self.subscription_requests: list[tuple[str, int]] = []

    def get_market_state(self, *, symbol: str) -> str:
        del symbol

        if self._states:
            self._last_state = self._states.pop(0)
        return self._last_state

    def ensure_order_book_subscription(self, *, symbol: str, depth: int) -> None:
        self.subscription_requests.append((symbol, depth))

    def get_order_book_snapshot(self, *, symbol: str, depth: int) -> OrderBookSnapshot:
        del symbol, depth
        return self._order_book

    def close(self) -> None:
        return None


class FakeTradeBroker:
    supports_native_ioc = False

    def __init__(self, latest_order: BrokerOrderSnapshot) -> None:
        self.latest_order = latest_order
        self.time_in_force = None

    def place_limit_buy(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price,
        trade_mode: TradeMode,
        time_in_force: TimeInForce,
        remark: str | None = None,
    ) -> BrokerOrderSnapshot:
        del symbol, quantity, limit_price, trade_mode, remark
        self.time_in_force = time_in_force
        return self.latest_order

    def get_order(self, *, order_id: str, symbol: str, trade_mode: TradeMode) -> BrokerOrderSnapshot:
        del order_id, symbol, trade_mode
        return self.latest_order

    def cancel_order(self, *, order_id: str, symbol: str, trade_mode: TradeMode) -> None:
        del order_id, symbol, trade_mode
        return None

    def close(self) -> None:
        return None


class GreyMarketSnatchTests(unittest.TestCase):
    def test_waits_until_market_state_is_tradable(self) -> None:
        order_book = OrderBookSnapshot(
            symbol="09868",
            asks=(QuoteLevel(price="3.28", quantity=100),),
        )
        market_data = FakeMarketDataClient(
            states=["WAITING_OPEN", "WAITING_OPEN", "AFTER_HOURS_BEGIN"],
            order_book=order_book,
        )
        clock = FakeClock()
        config = RuntimeConfig(
            quote_poll_interval_seconds=0.2,
            default_wait_for_open_timeout_seconds=5.0,
        )

        state, waited = wait_for_grey_market_open(
            "09868",
            market_data,
            config=config,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertEqual(state, "AFTER_HOURS_BEGIN")
        self.assertEqual(waited, 0.4)

    def test_run_snatch_can_plan_without_submitting(self) -> None:
        request = GreyMarketBuyRequest(symbol="09868", quantity=100)
        order_book = OrderBookSnapshot(
            symbol="09868",
            asks=(
                QuoteLevel(price="3.28", quantity=100),
                QuoteLevel(price="3.29", quantity=200),
            ),
        )
        market_data = FakeMarketDataClient(
            states=["AFTER_HOURS_BEGIN"],
            order_book=order_book,
        )
        config = RuntimeConfig(default_order_book_depth=5)

        report = run_grey_market_snatch(
            request,
            market_data,
            broker=None,
            config=config,
        )

        self.assertFalse(report.submitted)
        self.assertEqual(report.market_state, "AFTER_HOURS_BEGIN")
        self.assertEqual(report.plan.selected_limit_price, report.plan.minimum_limit_price)
        self.assertEqual(market_data.subscription_requests, [("09868", 5)])

    def test_run_snatch_can_submit_via_broker(self) -> None:
        request = GreyMarketBuyRequest(symbol="09868", quantity=100)
        order_book = OrderBookSnapshot(
            symbol="09868",
            asks=(QuoteLevel(price="3.28", quantity=100),),
        )
        market_data = FakeMarketDataClient(
            states=["AFTER_HOURS_BEGIN"],
            order_book=order_book,
        )
        broker = FakeTradeBroker(
            BrokerOrderSnapshot(
                order_id="1",
                symbol="09868",
                status=BrokerOrderStatus.FILLED_ALL,
                quantity=100,
                price="3.28",
                dealt_quantity=100,
                dealt_avg_price="3.28",
            )
        )

        report = run_grey_market_snatch(
            request,
            market_data,
            broker=broker,
            config=RuntimeConfig(),
        )

        self.assertTrue(report.submitted)
        self.assertEqual(report.execution_report.latest_order.status, BrokerOrderStatus.FILLED_ALL)
        self.assertEqual(broker.time_in_force, TimeInForce.DAY)

    def test_wait_times_out(self) -> None:
        order_book = OrderBookSnapshot(
            symbol="09868",
            asks=(QuoteLevel(price="3.28", quantity=100),),
        )
        market_data = FakeMarketDataClient(
            states=["WAITING_OPEN"],
            order_book=order_book,
        )
        clock = FakeClock()
        config = RuntimeConfig(
            quote_poll_interval_seconds=0.2,
            default_wait_for_open_timeout_seconds=0.5,
        )

        with self.assertRaises(MarketDataTimeoutError):
            wait_for_grey_market_open(
                "09868",
                market_data,
                config=config,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

    def test_load_futu_module_can_prepare_home_override(self) -> None:
        fake_module = object()
        with tempfile.TemporaryDirectory() as temp_home:
            config = RuntimeConfig(futu_sdk_home_override=temp_home)
            with patch(
                "futu_opend_execution.execution.futu_runtime.importlib.import_module",
                return_value=fake_module,
            ):
                loaded = load_futu_module(config)

            self.assertIs(loaded, fake_module)


if __name__ == "__main__":
    unittest.main()
