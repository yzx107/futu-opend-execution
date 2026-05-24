from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from futu_opend_execution.agent.futures_runtime import FuturesReplayConfig, run_futures_replay
from futu_opend_execution.cli.main import _futures_fixture_events
from futu_opend_execution.contracts import ContractSpec
from futu_opend_execution.data.futures_csv import FuturesCsvReplayProvider
from futu_opend_execution.data.market import MarketState
from futu_opend_execution.ledger.futures import summarize_futures_paper_ledger
from futu_opend_execution.strategies.futures_mean_reversion import (
    FuturesMeanReversionRules,
    FuturesMeanReversionStrategy,
    FuturesRiskSnapshot,
    FuturesSignalStatus,
    FuturesStrategyAction,
)


def _contract() -> ContractSpec:
    return ContractSpec(
        symbol="HK.HSI2606",
        exchange="HKFE",
        asset_class="INDEX_FUTURE",
        contract_multiplier="50",
        tick_size="1",
        margin_rate="0.08",
        commission_per_contract="12",
    )


def _state(**updates) -> MarketState:
    base = {
        "symbol": "HK.HSI2606",
        "timestamp": datetime(2026, 5, 21, 9, 15, 10),
        "interval_seconds": 1,
        "last_price": Decimal("19960"),
        "best_bid": Decimal("19959"),
        "bid_size": Decimal("10"),
        "best_ask": Decimal("19960"),
        "ask_size": Decimal("10"),
        "spread_bps": Decimal("0.5"),
        "orderbook_imbalance": Decimal("0"),
        "opening_vwap": Decimal("20000"),
        "rolling_vwap": Decimal("19995"),
        "realized_vol": Decimal("20"),
        "rolling_high": Decimal("20000"),
        "rolling_low": Decimal("19960"),
        "cumulative_volume": Decimal("10"),
        "cumulative_turnover": Decimal("200000"),
        "volume_delta": Decimal("1"),
        "turnover_delta": Decimal("19960"),
        "tick_count": 10,
        "source": "fixture",
    }
    base.update(updates)
    return MarketState(**base)


class FuturesReplayStrategyTests(unittest.TestCase):
    def test_csv_provider_maps_rows_to_market_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "futures.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "price", "volume", "bid_price", "bid_size", "ask_price", "ask_size"])
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": "2026-05-21T09:15:00",
                        "symbol": "HK.HSI2606",
                        "price": "20000",
                        "volume": "1",
                        "bid_price": "19999",
                        "bid_size": "10",
                        "ask_price": "20000",
                        "ask_size": "10",
                    }
                )

            states = list(FuturesCsvReplayProvider(path=path, symbol="HK.HSI2606").iter_market_states())

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].symbol, "HK.HSI2606")
        self.assertEqual(states[0].best_bid, Decimal("19999"))

    def test_strategy_opens_long_below_vwap_and_closes_near_vwap(self) -> None:
        strategy = FuturesMeanReversionStrategy(contract=_contract())
        open_signal = strategy.evaluate(market=_state(), risk=FuturesRiskSnapshot())
        close_signal = strategy.evaluate(
            market=_state(last_price=Decimal("19994"), best_bid=Decimal("19993"), best_ask=Decimal("19994")),
            risk=FuturesRiskSnapshot(open_long=1),
        )

        self.assertEqual(open_signal.action, FuturesStrategyAction.BUY_OPEN)
        self.assertEqual(open_signal.status, FuturesSignalStatus.DRY_RUN_SIGNAL)
        self.assertEqual(close_signal.action, FuturesStrategyAction.SELL_CLOSE)

    def test_strategy_blocks_wide_spread_and_proposed_margin(self) -> None:
        spread_block = FuturesMeanReversionStrategy(contract=_contract()).evaluate(
            market=_state(spread_bps=Decimal("50")),
            risk=FuturesRiskSnapshot(),
        )
        margin_block = FuturesMeanReversionStrategy(
            contract=_contract(),
            rules=FuturesMeanReversionRules(max_margin_used="100"),
        ).evaluate(market=_state(), risk=FuturesRiskSnapshot())

        self.assertEqual(spread_block.status, FuturesSignalStatus.RISK_BLOCKED)
        self.assertEqual(margin_block.reason, "proposed margin exceeds max")

    def test_run_futures_replay_fixture_writes_paper_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "replay.jsonl"
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            provider = FuturesCsvReplayProvider.from_events(_futures_fixture_events("HK.HSI2606"))
            summary = run_futures_replay(
                config=FuturesReplayConfig(contract=_contract()),
                market_states=provider.iter_market_states(),
                log_path=log_path,
                ledger_path=ledger_path,
            )
            ledger_summary = summarize_futures_paper_ledger(ledger_path, contracts={"HK.HSI2606": _contract()})
            self.assertTrue(log_path.exists())

        self.assertEqual(summary["event"], "futures_replay_summary")
        self.assertGreaterEqual(summary["total_signals"], 1)
        self.assertGreaterEqual(ledger_summary["fill_count"], 1)


if __name__ == "__main__":
    unittest.main()
