from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from futu_opend_execution.data.hshare_l2 import HshareL2ReplayProvider
from futu_opend_execution.data.market import MarketEvent, build_market_states


class MarketDataTests(unittest.TestCase):
    def test_market_state_builder_computes_spread_imbalance_vwap_and_vol(self) -> None:
        start = datetime(2026, 5, 21, 9, 30)
        states = build_market_states(
            [
                MarketEvent("00700", start, "trade", price="100", volume=100),
                MarketEvent("00700", start, "book", bid_price="99.9", bid_size=300, ask_price="100.1", ask_size=100),
                MarketEvent("00700", start + timedelta(seconds=1), "trade", price="101", volume=100),
            ],
            interval_seconds=1,
        )

        self.assertEqual(len(states), 2)
        self.assertEqual(states[0].symbol, "HK.00700")
        self.assertEqual(states[0].opening_vwap, Decimal("100"))
        self.assertEqual(states[0].orderbook_imbalance, Decimal("0.5"))
        self.assertGreater(states[0].spread_bps, 0)
        self.assertEqual(states[1].opening_vwap, Decimal("100.5"))
        self.assertGreater(states[1].realized_vol, 0)

    def test_in_memory_replay_provider_sorts_events_and_generates_states(self) -> None:
        start = datetime(2026, 5, 21, 9, 30)
        provider = HshareL2ReplayProvider.from_events(
            [
                MarketEvent("HK.00700", start + timedelta(seconds=1), "trade", price="101", volume=100),
                MarketEvent("HK.00700", start, "trade", price="100", volume=100),
            ],
            interval_seconds=1,
        )

        states = list(provider.iter_market_states())

        self.assertEqual([state.last_price for state in states], [Decimal("100"), Decimal("101")])


if __name__ == "__main__":
    unittest.main()
