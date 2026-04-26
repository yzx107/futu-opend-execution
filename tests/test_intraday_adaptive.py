from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.grey_open import GreyMarketSignal
from futu_opend_execution.signals.intraday_adaptive import IntradayAdaptiveTracker


class AdaptiveTrackerTests(unittest.TestCase):
    def test_handles_missing_aggressor_and_updates_metrics(self) -> None:
        tracker = IntradayAdaptiveTracker(window_size=5)
        signal = GreyMarketSignal(
            symbol="HK.01234",
            dark_status="TRADING",
            best_bid="9.98",
            best_ask="10.00",
            bid_quantity=1000,
            ask_quantity=1200,
            raw_quote={"last_price": "9.99", "turnover": "100000", "volume": "10000"},
            raw_order_book={"Ask": [["10.00", 1200, 1]], "Bid": [["9.98", 1000, 1]]},
        )
        state = tracker.update_from_signal(signal)

        self.assertEqual(state.tick_count, 1)
        self.assertGreater(state.spread_bps, 0)
        self.assertLess(state.orderbook_imbalance, 0)

    def test_cumulative_volume_turnover_does_not_double_count(self) -> None:
        tracker = IntradayAdaptiveTracker(window_size=5)

        first = tracker.update_from_signal(
            GreyMarketSignal(
                symbol="HK.01234",
                dark_status="TRADING",
                best_bid="10.00",
                best_ask="10.02",
                raw_quote={"last_price": "10.01", "turnover": "10000", "volume": "1000"},
            )
        )
        second = tracker.update_from_signal(
            GreyMarketSignal(
                symbol="HK.01234",
                dark_status="TRADING",
                best_bid="10.10",
                best_ask="10.12",
                raw_quote={"last_price": "10.11", "turnover": "12020", "volume": "1200"},
            )
        )
        reset = tracker.update_from_signal(
            GreyMarketSignal(
                symbol="HK.01234",
                dark_status="TRADING",
                best_bid="10.05",
                best_ask="10.07",
                raw_quote={"last_price": "10.06", "turnover": "11000", "volume": "1100"},
            )
        )
        third = tracker.update_from_signal(
            GreyMarketSignal(
                symbol="HK.01234",
                dark_status="TRADING",
                best_bid="10.20",
                best_ask="10.22",
                raw_quote={"last_price": "10.21", "turnover": "13042", "volume": "1300"},
            )
        )

        self.assertIsNone(first.opening_vwap)
        self.assertEqual(second.opening_vwap, Decimal("10.1"))
        self.assertEqual(reset.opening_vwap, Decimal("10.1"))
        self.assertEqual(third.opening_vwap, Decimal("10.155"))
        self.assertEqual(third.rolling_vwap, Decimal("10.16"))


if __name__ == "__main__":
    unittest.main()
