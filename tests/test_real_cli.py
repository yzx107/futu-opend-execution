from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.cli.main import main


def _approval_payload():
    return {
        "approval_id": "approval-cli",
        "signal_id": "signal-cli",
        "symbol": "HK.00700",
        "side": "SELL",
        "role": "TRADING_SELL",
        "quantity": 100,
        "limit_price": "300.00",
        "expected_edge_bps": "50",
        "created_at": "2026-05-23T09:30:00+00:00",
        "expires_at": "2099-05-23T09:35:00+00:00",
        "approved": True,
        "approved_by_operator": "operator",
        "confirmation_phrase": "确认实盘",
        "lot_size": 100,
        "market_snapshot": {
            "stale": False,
            "spread_bps": "5",
            "max_spread_bps": "20",
            "best_bid": "300.00",
            "best_ask": "300.20",
        },
        "inventory_snapshot": {
            "trading_available_to_sell": 100,
            "trading_available_to_rebuy": 0,
        },
        "risk_snapshot": {"max_severity": "INFO", "has_critical": False},
    }


class RealCliTests(unittest.TestCase):
    def _write_approval(self, directory: str) -> Path:
        path = Path(directory) / "approval.json"
        path.write_text(json.dumps(_approval_payload(), ensure_ascii=False), encoding="utf-8")
        return path

    def _run(self, argv) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, json.loads(buffer.getvalue())

    def test_validate_approval_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            approval = self._write_approval(temp_dir)

            code, payload = self._run(["real", "validate-approval", "--approval-file", str(approval)])

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_submit_approved_rejects_without_real_trade_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            approval = self._write_approval(temp_dir)
            audit = Path(temp_dir) / "audit.jsonl"

            code, payload = self._run([
                "real",
                "submit-approved",
                "--approval-file",
                str(approval),
                "--confirm-text",
                "确认实盘",
                "--audit-log",
                str(audit),
                "--fake-broker",
            ])

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "REJECTED")

    def test_submit_approved_fake_broker_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"FUTU_ALLOW_REAL_TRADE": "1"}):
            approval = self._write_approval(temp_dir)
            audit = Path(temp_dir) / "audit.jsonl"

            code, payload = self._run([
                "real",
                "submit-approved",
                "--approval-file",
                str(approval),
                "--confirm-text",
                "确认实盘",
                "--audit-log",
                str(audit),
                "--fake-broker",
            ])

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["filled_quantity"], 100)


if __name__ == "__main__":
    unittest.main()
