from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.contracts import ContractSpec
from futu_opend_execution.ledger.futures import FuturesLedgerError, FuturesPaperLedger, summarize_futures_paper_ledger


def _contracts():
    spec = ContractSpec(
        symbol="HK.HSI2606",
        exchange="HKFE",
        asset_class="INDEX_FUTURE",
        contract_multiplier="50",
        tick_size="1",
        margin_rate="0.08",
        commission_per_contract="12",
    )
    return {spec.symbol: spec}


class FuturesPaperLedgerTests(unittest.TestCase):
    def test_long_open_close_realizes_fifo_pnl_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "futures.jsonl"
            ledger = FuturesPaperLedger(path, contracts=_contracts())
            open_row = ledger.record_fill(symbol="HK.HSI2606", action="BUY_OPEN", quantity=1, price="20000", event_id="o1")
            duplicate = ledger.record_fill(symbol="HK.HSI2606", action="BUY_OPEN", quantity=1, price="20000", event_id="o1")
            close_row = ledger.record_fill(symbol="HK.HSI2606", action="SELL_CLOSE", quantity=1, price="20010", event_id="c1")

            self.assertEqual(open_row["entry_type"], "OPEN")
            self.assertIsNone(duplicate)
            self.assertEqual(close_row["gross_pnl"], "500")
            self.assertEqual(close_row["net_pnl"], "488")
            self.assertEqual(ledger.open_positions, {})

            summary = summarize_futures_paper_ledger(path, contracts=_contracts())
            self.assertEqual(summary["realized_gross_pnl"], "500")
            self.assertEqual(summary["total_commission"], "24")
            self.assertEqual(summary["realized_net_pnl"], "476")

    def test_short_open_close_and_mark_to_market_margin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "futures.jsonl"
            ledger = FuturesPaperLedger(path, contracts=_contracts())
            ledger.record_fill(symbol="HK.HSI2606", action="SELL_OPEN", quantity=2, price="20000", event_id="s1")
            summary = ledger.summary(mark_prices={"HK.HSI2606": "19990"})

            self.assertEqual(summary["open_positions"], {"HK.HSI2606": {"LONG": 0, "SHORT": 2}})
            self.assertEqual(summary["unrealized_gross_pnl"], "1000")
            self.assertEqual(summary["margin_used"], "159920")

            close_row = ledger.record_fill(symbol="HK.HSI2606", action="BUY_CLOSE", quantity=2, price="19980", event_id="b1")
            self.assertEqual(close_row["gross_pnl"], "2000")

    def test_rejects_overclose_and_unaligned_price(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "futures.jsonl"
            ledger = FuturesPaperLedger(path, contracts=_contracts())

            with self.assertRaisesRegex(FuturesLedgerError, "exceeds open position"):
                ledger.record_fill(symbol="HK.HSI2606", action="SELL_CLOSE", quantity=1, price="20000")
            with self.assertRaisesRegex(ValueError, "tick_size"):
                ledger.record_fill(symbol="HK.HSI2606", action="BUY_OPEN", quantity=1, price="20000.5")


if __name__ == "__main__":
    unittest.main()
