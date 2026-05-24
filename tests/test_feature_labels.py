from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from futu_opend_execution.agent.feature_labels import (
    FeatureLabelRules,
    build_feature_label_rows,
    evaluate_newly_listed_feature_labels,
    write_feature_label_reports,
)
from futu_opend_execution.data.market import MarketState


class FeatureLabelTests(unittest.TestCase):
    def test_feature_rows_label_future_sell_rebuy_and_buy_sell_edges(self) -> None:
        rows = build_feature_label_rows(
            [
                _state(0, "100", bid="99.9", ask="100.1"),
                _state(1, "98", bid="97.9", ask="98.1"),
                _state(2, "101", bid="100.9", ask="101.1"),
            ],
            symbol="HK.01609",
            trade_date="2026-05-22",
            listing_date="2026-05-05",
            rules=FeatureLabelRules(horizons_seconds=(5,), cost_bps=Decimal("35")),
        )

        first = rows[0]["labels"]["5"]
        second = rows[1]["labels"]["5"]
        self.assertGreater(Decimal(first["sell_rebuy_edge_bps"]), Decimal("0"))
        self.assertGreater(Decimal(second["buy_sell_edge_bps"]), Decimal("0"))
        self.assertEqual(rows[0]["days_since_listing"], 17)
        self.assertEqual(rows[0]["quality_ok"], True)

    def test_newly_listed_feature_label_summary_marks_candidate_from_fake_states(self) -> None:
        def fake_market_states(symbol, trade_date, data_root, top_of_book_root):
            return [
                _state(0, "100", bid="99.9", ask="100.1"),
                _state(1, "98", bid="97.9", ask="98.1"),
                _state(2, "101", bid="100.9", ask="101.1"),
            ]

        import futu_opend_execution.agent.feature_labels as labels

        original_universe = labels.build_newly_listed_universe
        original_market_states = labels._market_states
        try:
            labels.build_newly_listed_universe = lambda **_: {
                "candidate_count": 1,
                "candidates": [
                    {"symbol": "HK.01609", "listing_date": "2026-05-05", "available_trade_dates": ["2026-05-22"]}
                ],
            }
            labels._market_states = fake_market_states
            summary = evaluate_newly_listed_feature_labels(
                rules=FeatureLabelRules(
                    horizons_seconds=(5,),
                    cost_bps=Decimal("35"),
                    min_group_count=1,
                    min_group_symbols=1,
                    min_hit_rate=Decimal("0.5"),
                    min_avg_edge_bps=Decimal("0"),
                ),
                keep_rows=True,
            )
        finally:
            labels.build_newly_listed_universe = original_universe
            labels._market_states = original_market_states

        self.assertEqual(summary["event"], "newly_listed_feature_label_summary")
        self.assertEqual(summary["decision"], "CANDIDATE")
        self.assertGreater(summary["feature_row_count"], 0)
        self.assertIn(summary["recommended_candidate"]["direction"], {"SELL_REBUY", "BUY_SELL"})

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_feature_label_reports(
                summary,
                json_path=root / "summary.json",
                markdown_path=root / "summary.md",
                rows_jsonl_path=root / "rows.jsonl",
            )
            stored = json.loads((root / "summary.json").read_text(encoding="utf-8"))
            self.assertNotIn("feature_label_rows", stored)
            self.assertIn("Newly Listed Feature Labels", (root / "summary.md").read_text(encoding="utf-8"))
            self.assertEqual(len((root / "rows.jsonl").read_text(encoding="utf-8").splitlines()), summary["feature_row_count"])


def _state(offset: int, price: str, *, bid: str | None = None, ask: str | None = None, quality: str = "OK") -> MarketState:
    value = Decimal(price)
    bid_value = Decimal(bid) if bid is not None else value - Decimal("0.1")
    ask_value = Decimal(ask) if ask is not None else value + Decimal("0.1")
    return MarketState(
        symbol="HK.01609",
        timestamp=datetime(2026, 5, 22, 9, 30) + timedelta(seconds=offset),
        interval_seconds=1,
        last_price=value,
        best_bid=bid_value,
        bid_size=Decimal("100"),
        best_ask=ask_value,
        ask_size=Decimal("100"),
        spread_bps=Decimal("20"),
        orderbook_imbalance=Decimal("0.3"),
        opening_vwap=Decimal("100"),
        rolling_vwap=Decimal("100"),
        realized_vol=Decimal("1"),
        rolling_high=max(value, Decimal("100")),
        rolling_low=min(value, Decimal("100")),
        cumulative_volume=Decimal("100"),
        cumulative_turnover=value * Decimal("100"),
        volume_delta=Decimal("100"),
        turnover_delta=value * Decimal("100"),
        tick_count=10,
        source="fixture",
        book_quality=quality,
    )


if __name__ == "__main__":
    unittest.main()
