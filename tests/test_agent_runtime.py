from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.agent.runtime import TradingAgentConfig, run_monitor, run_paper, run_replay
from futu_opend_execution.cli.main import build_parser
from futu_opend_execution.data.hshare_l2 import HshareL2ReplayProvider
from futu_opend_execution.cli.main import _fixture_events


class AgentRuntimeTests(unittest.TestCase):
    def test_cli_parser_accepts_core_commands(self) -> None:
        parser = build_parser()
        replay = parser.parse_args(["replay", "HK.00700", "--current-qty", "200", "--cost-price", "100", "--lot-size", "100", "--fixture"])
        monitor = parser.parse_args(["monitor", "00700", "--current-qty", "200", "--cost-price", "100", "--lot-size", "100", "--fake"])

        self.assertEqual(replay.command, "replay")
        self.assertEqual(monitor.command, "monitor")

    def test_replay_emits_summary_and_paper_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "replay.jsonl"
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            report_path = Path(temp_dir) / "paper.json"
            config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="100", lot_size=100)
            provider = HshareL2ReplayProvider.from_events(_fixture_events("HK.00700"), interval_seconds=1)

            summary = run_replay(config=config, market_states=provider.iter_market_states(), log_path=log_path)
            paper = run_paper(replay_log_path=log_path, ledger_path=ledger_path, report_path=report_path)

            self.assertEqual(summary["event"], "replay_summary")
            self.assertTrue(log_path.exists())
            self.assertTrue(report_path.exists())
            self.assertIn("round_trips", paper)

    def test_monitor_fake_provider_runs_one_iteration(self) -> None:
        from futu_opend_execution.cli.main import _FakeLiveProvider

        with tempfile.TemporaryDirectory() as temp_dir:
            config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="100", lot_size=100)
            events = run_monitor(config=config, provider=_FakeLiveProvider("HK.00700"), log_path=Path(temp_dir) / "monitor.jsonl", iterations=1)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "strategy_signal")


if __name__ == "__main__":
    unittest.main()
