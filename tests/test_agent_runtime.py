from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.agent.runtime import TradingAgentConfig, run_monitor, run_paper, run_replay, run_watchlist_monitor
from futu_opend_execution.cli.main import build_parser
from futu_opend_execution.data.hshare_l2 import HshareL2ReplayProvider
from futu_opend_execution.cli.main import _fixture_events


class AgentRuntimeTests(unittest.TestCase):
    def test_cli_parser_accepts_core_commands(self) -> None:
        parser = build_parser()
        replay = parser.parse_args([
            "replay",
            "HK.00700",
            "--current-qty",
            "200",
            "--cost-price",
            "100",
            "--lot-size",
            "100",
            "--date",
            "2026-05-22",
            "--top-of-book-root",
            "/tmp/top",
        ])
        monitor = parser.parse_args(["monitor", "00700", "--current-qty", "200", "--cost-price", "100", "--lot-size", "100", "--fake"])
        watchlist = parser.parse_args(["watchlist", "validate", "--config", "configs/watchlist.example.json"])
        optimize = parser.parse_args([
            "optimize-cost-reducer",
            "HK.00700",
            "--current-qty",
            "200",
            "--cost-price",
            "100",
            "--lot-size",
            "100",
            "--fixture",
            "--overextension-grid",
            "1.5,2.0",
        ])
        universe = parser.parse_args(["newly-listed-universe", "--listing-year", "2026", "--date", "2026-05-22"])
        optimize_new = parser.parse_args(["optimize-newly-listed", "--listing-year", "2026", "--max-symbols", "1"])
        futures = parser.parse_args(["futures", "contracts", "--config", "configs/futures_contracts.example.json"])
        futures_info = parser.parse_args(["futures", "opend-info", "HK.HSI2606", "--check-trade-context"])
        futures_replay = parser.parse_args(["futures", "replay", "HK.HSI2606", "--fixture"])

        self.assertEqual(replay.command, "replay")
        self.assertEqual(replay.top_of_book_root, "/tmp/top")
        self.assertEqual(monitor.command, "monitor")
        self.assertEqual(watchlist.command, "watchlist")
        self.assertEqual(optimize.command, "optimize-cost-reducer")
        self.assertEqual(universe.command, "newly-listed-universe")
        self.assertEqual(optimize_new.command, "optimize-newly-listed")
        self.assertEqual(futures.command, "futures")
        self.assertEqual(futures.futures_command, "contracts")
        self.assertEqual(futures_info.futures_command, "opend-info")
        self.assertEqual(futures_replay.futures_command, "replay")

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

    def test_monitor_fake_provider_is_deterministic_and_emits_dry_run_signal(self) -> None:
        from futu_opend_execution.cli.main import _FakeLiveProvider

        with tempfile.TemporaryDirectory() as temp_dir:
            config = TradingAgentConfig("HK.00700", current_qty=200, cost_price="100", lot_size=100)
            log_path = Path(temp_dir) / "monitor.jsonl"
            events = run_monitor(config=config, provider=_FakeLiveProvider("HK.00700"), log_path=log_path, iterations=5, interval_seconds=0)

            self.assertTrue(any(event.get("status") == "DRY_RUN_SIGNAL" for event in events))
            market_rows = [line for line in log_path.read_text(encoding="utf-8").splitlines() if '"market_state"' in line]
            self.assertTrue(market_rows)

    def test_watchlist_monitor_fake_writes_risk_event_and_signal(self) -> None:
        from futu_opend_execution.cli.main import _FakeLiveProvider
        from futu_opend_execution.watchlist import WatchlistConfig

        config = WatchlistConfig.from_dict(
            {
                "symbols": [
                    {
                        "symbol": "HK.00700",
                        "enabled": True,
                        "lot_size": 100,
                        "current_qty": 200,
                        "cost_price": "100",
                        "core_qty_target": 100,
                        "trading_qty_target": 100,
                        "max_sell_qty_per_order": 100,
                        "max_rebuy_qty_per_order": 100,
                        "max_sell_total_position_ratio": "0.5",
                        "max_round_trips": 1,
                        "black_swan_thresholds": {
                            "intraday_drop_bps": "500",
                            "gap_down_bps": "800",
                            "spread_bps": "50",
                            "stale_seconds": "3",
                            "min_bid_size_lots": 0,
                        },
                        "cost_reducer_rules": {
                            "overextension_vol_multiple": "2.0",
                            "high_pullback_vol_multiple": "0.5",
                            "rebuy_anchor_vol_band": "1.0",
                            "max_spread_bps": "20",
                            "estimated_roundtrip_cost_bps": "35",
                            "safety_buffer_bps": "20",
                        },
                    }
                ]
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "monitor.jsonl"
            rows = run_watchlist_monitor(
                watchlist=config,
                provider=_FakeLiveProvider("HK.00700"),
                log_path=log_path,
                iterations=5,
                interval_seconds=0,
            )

            self.assertTrue(any(row.get("event") == "risk_event" for row in rows))
            self.assertTrue(any(row.get("status") == "DRY_RUN_SIGNAL" for row in rows))


if __name__ == "__main__":
    unittest.main()
