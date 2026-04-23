from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution import (
    GreyMarketBuyRequest,
    InsufficientLiquidityError,
    OrderBookSnapshot,
    PriceCapExceededError,
    QuoteLevel,
    RealTradeDisabledError,
    RuntimeConfig,
    TradeMode,
    build_grey_market_buy_plan,
)


class GreyMarketPlannerTests(unittest.TestCase):
    def test_uses_marginal_ask_as_lowest_fill_price(self) -> None:
        request = GreyMarketBuyRequest(symbol="09868", quantity=250)
        snapshot = OrderBookSnapshot(
            symbol="09868",
            asks=(
                QuoteLevel(price="3.28", quantity=100),
                QuoteLevel(price="3.29", quantity=200),
                QuoteLevel(price="3.30", quantity=400),
            ),
        )

        plan = build_grey_market_buy_plan(request, snapshot)

        self.assertEqual(plan.minimum_limit_price, Decimal("3.29"))
        self.assertEqual(plan.selected_limit_price, Decimal("3.29"))
        self.assertEqual(plan.expected_fill.filled_quantity, 250)
        self.assertEqual(
            plan.expected_fill.average_price,
            Decimal("821.5") / Decimal("250"),
        )

    def test_rejects_when_visible_liquidity_is_insufficient(self) -> None:
        request = GreyMarketBuyRequest(symbol="09868", quantity=500)
        snapshot = OrderBookSnapshot(
            symbol="09868",
            asks=(
                QuoteLevel(price="3.28", quantity=100),
                QuoteLevel(price="3.29", quantity=200),
            ),
        )

        with self.assertRaises(InsufficientLiquidityError):
            build_grey_market_buy_plan(request, snapshot)

    def test_respects_price_cap(self) -> None:
        request = GreyMarketBuyRequest(
            symbol="09868",
            quantity=250,
            max_limit_price="3.29",
            price_buffer_ticks=1,
            tick_size="0.01",
        )
        snapshot = OrderBookSnapshot(
            symbol="09868",
            asks=(
                QuoteLevel(price="3.28", quantity=100),
                QuoteLevel(price="3.29", quantity=200),
            ),
        )

        with self.assertRaises(PriceCapExceededError):
            build_grey_market_buy_plan(request, snapshot)

    def test_real_trade_requires_explicit_gate(self) -> None:
        request = GreyMarketBuyRequest(
            symbol="09868",
            quantity=100,
            trade_mode=TradeMode.REAL,
        )
        snapshot = OrderBookSnapshot(
            symbol="09868",
            asks=(QuoteLevel(price="3.28", quantity=100),),
        )
        config = RuntimeConfig(allow_real_trade=False)

        with self.assertRaises(RealTradeDisabledError):
            build_grey_market_buy_plan(request, snapshot, config=config)


if __name__ == "__main__":
    unittest.main()
