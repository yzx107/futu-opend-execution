from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from futu_opend_execution.agent.optimizer import CostReducerGrid, optimize_cost_reducer, write_optimizer_reports
from futu_opend_execution.agent.runtime import TradingAgentConfig
from futu_opend_execution.cli.main import _fixture_events
from futu_opend_execution.data.hshare_l2 import HshareL2ReplayProvider


class CostReducerOptimizerTests(unittest.TestCase):
    def test_optimizer_ranks_grid_and_writes_reports(self) -> None:
        config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="100", lot_size=100)
        provider = HshareL2ReplayProvider.from_events(_fixture_events("HK.00700"), interval_seconds=1)
        grid = CostReducerGrid(
            overextension_vol_multiple=(Decimal("1.5"), Decimal("2.0")),
            high_pullback_vol_multiple=(Decimal("0.3"),),
            rebuy_anchor_vol_band=(Decimal("1.0"),),
            safety_buffer_bps=(Decimal("20"),),
            max_sell_total_position_ratio=(Decimal("0.5"),),
            max_round_trips=(1,),
        )

        summary = optimize_cost_reducer(
            config=config,
            market_states=provider.iter_market_states(),
            grid=grid,
            top_n=2,
        )

        self.assertEqual(summary["event"], "optimizer_summary")
        self.assertEqual(summary["grid_size"], 2)
        self.assertEqual(len(summary["results"]), 2)
        self.assertGreaterEqual(int(summary["results"][0]["sell_count"]), 0)
        self.assertIn("net_pnl_after_cost", summary["results"][0])
        self.assertIn("open_quantity_penalty", summary["results"][0])

        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "summary.json"
            md_path = Path(temp_dir) / "rank.md"
            write_optimizer_reports(summary, json_path=json_path, markdown_path=md_path)

            self.assertTrue(json_path.exists())
            self.assertIn("Cost Reducer Optimizer", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
