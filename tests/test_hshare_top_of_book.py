from __future__ import annotations

import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from futu_opend_execution.data.hshare_top_of_book import HshareTopOfBookReplayProvider


@unittest.skipUnless(importlib.util.find_spec("polars"), "polars is required for parquet fixture")
class HshareTopOfBookReplayTests(unittest.TestCase):
    def test_provider_marks_top_of_book_quality_and_depth_limits(self) -> None:
        import polars as pl

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            partition = (
                root
                / "orderbook_replay__top_of_book_only"
                / "top_of_book_events"
                / "year=2026"
                / "date=2026-05-22"
                / "symbol=00700"
            )
            partition.mkdir(parents=True)
            pl.DataFrame(
                [
                    {
                        "SendTime": "2026-05-22T09:30:00",
                        "symbol": "HK.00700",
                        "TradePrice": 100.0,
                        "TradeVolume": 100,
                        "BestBidReplay": 99.9,
                        "BestAskReplay": 100.1,
                        "TopOfBookValidFlag": True,
                        "ReplayQualityScore": 1.0,
                        "CrossedWindowFlag": False,
                        "ReplayResidueFlag": False,
                        "ReplayWindowExcludedFlag": False,
                        "SameMillisecondBatchRiskFlag": False,
                    },
                    {
                        "SendTime": "2026-05-22T09:30:01",
                        "symbol": "HK.00700",
                        "TradePrice": 101.0,
                        "TradeVolume": 100,
                        "BestBidReplay": 101.5,
                        "BestAskReplay": 101.0,
                        "TopOfBookValidFlag": False,
                        "ReplayQualityScore": 0.0,
                        "CrossedWindowFlag": True,
                        "ReplayResidueFlag": True,
                        "ReplayWindowExcludedFlag": True,
                        "SameMillisecondBatchRiskFlag": False,
                    },
                ]
            ).write_parquet(partition / "part-00000.parquet")

            provider = HshareTopOfBookReplayProvider(
                data_root=root,
                dates=["2026-05-22"],
                symbols=["HK.00700"],
            )
            states = list(provider.iter_market_states())

            self.assertEqual(len(states), 2)
            self.assertEqual(states[0].source, "hshare_top_of_book")
            self.assertEqual(states[0].best_bid, Decimal("99.9"))
            self.assertEqual(states[0].best_ask, Decimal("100.1"))
            self.assertEqual(states[0].book_quality, "OK_TOP_OF_BOOK_ONLY")
            self.assertTrue(states[0].book_depth_limited)
            self.assertTrue(states[0].orderbook_limited)
            self.assertEqual(states[1].book_quality, "BLOCKED")
            self.assertTrue(states[1].book_crossed)
            self.assertTrue(states[1].book_residue)
            self.assertTrue(states[1].orderbook_limited)

    def test_provider_requires_strategy_handoff_eligibility_when_present(self) -> None:
        import polars as pl

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            partition = (
                root
                / "orderbook_replay__top_of_book_with_size_caveat"
                / "top_of_book_events"
                / "year=2026"
                / "date=2026-05-22"
                / "symbol=01609"
            )
            partition.mkdir(parents=True)
            pl.DataFrame(
                [
                    {
                        "SendTime": "2026-05-22T09:30:00",
                        "symbol": "HK.01609",
                        "TradePrice": 193.0,
                        "TradeVolume": 100,
                        "BestBidReplay": 192.9,
                        "BestBidSizeReplay": 200,
                        "BestAskReplay": 193.1,
                        "BestAskSizeReplay": 300,
                        "TopOfBookValidFlag": True,
                        "ReplayQualityScore": 1.0,
                        "CrossedWindowFlag": False,
                        "ReplayResidueFlag": False,
                        "ReplayWindowExcludedFlag": False,
                        "SameMillisecondBatchRiskFlag": False,
                        "StrategyHandoffEligibleFlag": True,
                    },
                    {
                        "SendTime": "2026-05-22T09:30:01",
                        "symbol": "HK.01609",
                        "TradePrice": 193.2,
                        "TradeVolume": 100,
                        "BestBidReplay": 193.1,
                        "BestBidSizeReplay": 200,
                        "BestAskReplay": 193.3,
                        "BestAskSizeReplay": 300,
                        "TopOfBookValidFlag": True,
                        "ReplayQualityScore": 1.0,
                        "CrossedWindowFlag": False,
                        "ReplayResidueFlag": False,
                        "ReplayWindowExcludedFlag": False,
                        "SameMillisecondBatchRiskFlag": False,
                        "StrategyHandoffEligibleFlag": False,
                    },
                ]
            ).write_parquet(partition / "part-00000.parquet")

            provider = HshareTopOfBookReplayProvider(
                data_root=root,
                dates=["2026-05-22"],
                symbols=["HK.01609"],
            )
            states = list(provider.iter_market_states())

            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].book_quality, "OK")
            self.assertFalse(states[0].book_depth_limited)
            self.assertEqual(states[0].bid_size, Decimal("200"))
            self.assertEqual(states[0].ask_size, Decimal("300"))


if __name__ == "__main__":
    unittest.main()
