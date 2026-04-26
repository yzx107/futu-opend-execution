from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
