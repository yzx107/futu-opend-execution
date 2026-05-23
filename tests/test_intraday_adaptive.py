from __future__ import annotations

import unittest
from types import SimpleNamespace
from decimal import Decimal

from futu_opend_execution.signals.intraday_adaptive import IntradayAdaptiveTracker


class IntradayAdaptiveTrackerTests(unittest.TestCase):
    def test_negative_cumulative_deltas_are_fail_closed(self) -> None:
        tracker = IntradayAdaptiveTracker(window_size=5)
        tracker.update_from_signal(_signal(price="10", turnover="10000", volume="1000"))
        second = tracker.update_from_signal(_signal(price="10.1", turnover="12020", volume="1200"))
        reset = tracker.update_from_signal(_signal(price="10.05", turnover="11000", volume="1100"))

        self.assertEqual(second.volume_delta, Decimal("200"))
        self.assertEqual(second.turnover_delta, Decimal("2020"))
        self.assertTrue(reset.cumulative_field_reset_detected)
        self.assertEqual(reset.volume_delta, Decimal("0"))
        self.assertEqual(reset.turnover_delta, Decimal("0"))
        self.assertEqual(reset.cumulative_turnover, Decimal("2020"))


def _signal(*, price: str, turnover: str, volume: str):
    return SimpleNamespace(
        best_bid=price,
        best_ask=str(Decimal(price) + Decimal("0.01")),
        bid_quantity=1000,
        ask_quantity=1000,
        raw_quote={"last_price": price, "turnover": turnover, "volume": volume},
    )


if __name__ == "__main__":
    unittest.main()
