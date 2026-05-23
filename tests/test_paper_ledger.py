from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from futu_opend_execution.ledger.paper import PaperLedger, summarize_paper_ledger


class PaperLedgerTests(unittest.TestCase):
    def test_sell_rebuy_roundtrip_and_idempotency(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = PaperLedger(path, roundtrip_cost_bps=Decimal("35"))
            sell = ledger.record_trade(symbol="HK.00700", action="SELL_TRADING", quantity=100, price="110", event_id="s1")
            duplicate = ledger.record_trade(symbol="HK.00700", action="SELL_TRADING", quantity=100, price="110", event_id="s1")
            rebuy = ledger.record_trade(symbol="HK.00700", action="REBUY_TRADING", quantity=100, price="100", event_id="r1")

            self.assertEqual(sell["entry_type"], "SELL_OPEN")
            self.assertIsNone(duplicate)
            self.assertEqual(rebuy["entry_type"], "ROUND_TRIP_CLOSE")
            self.assertEqual(summarize_paper_ledger(path)["round_trips"], 1)
            self.assertEqual(ledger.open_quantity, 0)

    def test_fifo_duplicate_and_risk_blocked_ignored(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = PaperLedger(path, roundtrip_cost_bps=Decimal("35"))
            ledger.record_trade(symbol="HK.00700", action="SELL_TRADING", quantity=100, price="110", event_id="s1")
            ledger.record_trade(symbol="HK.00700", action="SELL_TRADING", quantity=100, price="111", event_id="s2")
            ignored = ledger.record_trade(symbol="HK.00700", action="REBUY_TRADING", quantity=100, price="109", event_id="b1", status="RISK_BLOCKED")
            rebuy = ledger.record_trade(symbol="HK.00700", action="REBUY_TRADING", quantity=150, price="100", event_id="r1")

            self.assertIsNone(ignored)
            self.assertEqual(rebuy["quantity"], 150)
            self.assertEqual(rebuy["matched_sells"][0]["price"], "110")
            self.assertEqual(rebuy["matched_sells"][1]["price"], "111")
            summary = summarize_paper_ledger(path)
            self.assertEqual(summary["sell_count"], 2)
            self.assertEqual(summary["rebuy_count"], 1)
            self.assertEqual(summary["open_quantity"], 50)


if __name__ == "__main__":
    unittest.main()
