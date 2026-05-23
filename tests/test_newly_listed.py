from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.agent.newly_listed import build_newly_listed_universe, write_newly_listed_reports


@unittest.skipUnless(importlib.util.find_spec("polars"), "polars is required for parquet fixture")
class NewlyListedTests(unittest.TestCase):
    def test_universe_filters_2026_stock_candidates_with_trade_data(self) -> None:
        import polars as pl

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "instrument_profile.parquet"
            trades = root / "candidate_cleaned" / "trades" / "date=2026-05-22"
            trades.mkdir(parents=True)
            pl.DataFrame(
                [
                    {
                        "instrument_key": "01609",
                        "listing_date": "2026-05-01",
                        "observed_first_date": "2026-05-01",
                        "observed_last_date": "2026-05-22",
                        "observed_trades_days": 2,
                        "observed_orders_days": 2,
                        "instrument_family": "listed_security_unclassified",
                        "stock_research_candidate": True,
                        "stock_research_candidate_status": "candidate",
                        "source_label": "fixture",
                    },
                    {
                        "instrument_key": "09999",
                        "listing_date": "2025-12-31",
                        "observed_first_date": "2026-05-01",
                        "observed_last_date": "2026-05-22",
                        "observed_trades_days": 2,
                        "observed_orders_days": 2,
                        "instrument_family": "listed_security_unclassified",
                        "stock_research_candidate": True,
                        "stock_research_candidate_status": "candidate",
                        "source_label": "fixture",
                    },
                ]
            ).with_columns(
                pl.col("listing_date").str.strptime(pl.Date),
                pl.col("observed_first_date").str.strptime(pl.Date),
                pl.col("observed_last_date").str.strptime(pl.Date),
            ).write_parquet(profile)
            pl.DataFrame(
                {
                    "SendTime": ["2026-05-22T09:30:00"],
                    "Price": [100.0],
                    "Volume": [100],
                    "source_file": ["trade/01609.csv"],
                }
            ).write_parquet(trades / "20260522_trades.parquet")

            summary = build_newly_listed_universe(
                instrument_profile_path=profile,
                data_root=root / "candidate_cleaned",
                listing_year=2026,
                dates=["2026-05-22"],
            )

            self.assertEqual(summary["candidate_count"], 1)
            self.assertEqual(summary["candidates"][0]["symbol"], "HK.01609")

            json_path = root / "universe.json"
            md_path = root / "universe.md"
            write_newly_listed_reports(summary, json_path=json_path, markdown_path=md_path)
            self.assertIn("HK.01609", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
