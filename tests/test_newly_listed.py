from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.agent.newly_listed import build_newly_listed_universe, build_walk_forward_summary, write_newly_listed_reports


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

    def test_universe_path_filters_included_hshare_rows_with_top_of_book_coverage(self) -> None:
        import polars as pl

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe = root / "newly_listed_hk_2026.parquet"
            top = root / "caveat"
            (
                top
                / "orderbook_replay__top_of_book_with_size_caveat"
                / "top_of_book_events"
                / "year=2026"
                / "date=2026-05-22"
                / "symbol=01609"
            ).mkdir(parents=True)
            pl.DataFrame(
                [
                    {
                        "symbol": "HK.01609",
                        "instrument_key": "01609",
                        "listing_date": "2026-05-05",
                        "source_label": "fixture",
                        "stock_research_candidate": True,
                        "candidate_cleaned_trade_dates": [["2026-05-22"]],
                        "candidate_cleaned_order_dates": [["2026-05-22"]],
                        "first_trade_date": "2026-05-22",
                        "last_trade_date": "2026-05-22",
                        "coverage_days": 1,
                        "universe_status": "included",
                        "caveat": "fixture included",
                    },
                    {
                        "symbol": "HK.01879",
                        "instrument_key": "01879",
                        "listing_date": "2019-03-29",
                        "source_label": "fixture",
                        "stock_research_candidate": True,
                        "candidate_cleaned_trade_dates": [["2026-05-22"]],
                        "candidate_cleaned_order_dates": [["2026-05-22"]],
                        "first_trade_date": "2026-05-22",
                        "last_trade_date": "2026-05-22",
                        "coverage_days": 1,
                        "universe_status": "watched",
                        "caveat": "not newly listed",
                    },
                ]
            ).write_parquet(universe)

            summary = build_newly_listed_universe(
                universe_path=universe,
                top_of_book_root=top,
                listing_year=2026,
                dates=["2026-05-22"],
            )

            self.assertEqual(summary["source"], str(universe))
            self.assertEqual(summary["candidate_count"], 1)
            self.assertEqual(summary["candidates"][0]["symbol"], "HK.01609")
            self.assertEqual(summary["candidates"][0]["universe_status"], "included")


class NewlyListedWalkForwardTests(unittest.TestCase):
    def test_walk_forward_summary_splits_train_and_validation_rankings(self) -> None:
        base = {
            "listing_year": 2026,
            "universe": {"candidate_count": 1},
            "evaluated_case_count": 2,
            "result_row_count": 4,
            "failure_count": 0,
            "failures": [],
            "assumptions": {"position_model": "fixture"},
            "per_case_results": [
                _result("2026-05-20", "1.5", "10", "10", 0),
                _result("2026-05-20", "2.0", "20", "20", 0),
                _result("2026-05-22", "1.5", "30", "30", 0),
                _result("2026-05-22", "2.0", "-5", "-5", 1),
            ],
        }

        summary = build_walk_forward_summary(base, validation_days=1, top_n=2)

        self.assertEqual(summary["event"], "newly_listed_walk_forward_summary")
        self.assertEqual(summary["split"]["train_dates"], ["2026-05-20"])
        self.assertEqual(summary["split"]["validation_dates"], ["2026-05-22"])
        self.assertEqual(summary["train_ranking"][0]["params"]["overextension_vol_multiple"], "2.0")
        self.assertEqual(summary["walk_forward_ranking"][0]["params"]["overextension_vol_multiple"], "1.5")
        self.assertEqual(summary["walk_forward_ranking"][0]["validation"]["score_sum"], "30")


def _result(date: str, overextension: str, score: str, net_pnl: str, open_quantity: int) -> dict[str, object]:
    return {
        "symbol": "HK.01609",
        "date": date,
        "listing_date": "2026-05-05",
        "params": {
            "overextension_vol_multiple": overextension,
            "high_pullback_vol_multiple": "0.3",
            "rebuy_anchor_vol_band": "1.0",
            "safety_buffer_bps": "20",
            "max_sell_total_position_ratio": "0.5",
            "max_round_trips": 1,
        },
        "sell_count": 1,
        "rebuy_count": 1,
        "round_trips_completed": 1,
        "open_quantity": open_quantity,
        "open_quantity_penalty": "0",
        "realized_net_pnl": net_pnl,
        "net_pnl_after_cost": net_pnl,
        "initial_cost_basis": "100",
        "final_economic_cost_basis": "99",
        "cost_basis_reduction": "1",
        "risk_block_count": 0,
        "quality_block_count": 0,
        "score": score,
    }


if __name__ == "__main__":
    unittest.main()
