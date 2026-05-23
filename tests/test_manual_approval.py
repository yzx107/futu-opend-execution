from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from futu_opend_execution.agent.approval import (
    PendingRealOrderApproval,
    approval_validation_errors,
    validate_approval,
)
from futu_opend_execution.risk import ExecutionValidationError


NOW = datetime(2026, 5, 23, 9, 31, tzinfo=timezone.utc)


def _payload(**updates):
    payload = {
        "approval_id": "approval-1",
        "signal_id": "signal-1",
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
    payload.update(updates)
    return payload


class ManualApprovalTests(unittest.TestCase):
    def _approval(self, **updates) -> PendingRealOrderApproval:
        return PendingRealOrderApproval.from_dict(_payload(**updates))

    def test_valid_approval_passes_static_validation(self) -> None:
        approval = self._approval()

        errors = approval_validation_errors(approval, now=NOW)

        self.assertEqual(errors, [])
        self.assertEqual(validate_approval(approval, now=NOW, require_approved=True).approval_id, "approval-1")

    def test_stale_and_expired_approval_rejected(self) -> None:
        stale = self._approval(market_snapshot={**_payload()["market_snapshot"], "stale": True})
        expired = self._approval(expires_at="2026-05-23T09:30:30+00:00")

        self.assertIn("market snapshot is stale", approval_validation_errors(stale, now=NOW))
        self.assertIn("approval is expired", approval_validation_errors(expired, now=NOW))

    def test_changed_quantity_or_price_rejected_against_signal_snapshot(self) -> None:
        changed_qty = self._approval(signal_snapshot={"quantity": 200})
        changed_price = self._approval(signal_snapshot={"limit_price": "301.00"})

        self.assertIn("approval quantity differs from signal snapshot", approval_validation_errors(changed_qty, now=NOW))
        self.assertIn("approval limit_price differs from signal snapshot", approval_validation_errors(changed_price, now=NOW))

    def test_critical_risk_and_wrong_confirmation_rejected(self) -> None:
        critical = self._approval(risk_snapshot={"max_severity": "CRITICAL", "has_critical": True})
        wrong_phrase = self._approval(confirmation_phrase="wrong")

        self.assertIn("critical risk blocks approval", approval_validation_errors(critical, now=NOW))
        self.assertIn("confirmation phrase is incorrect", approval_validation_errors(wrong_phrase, now=NOW))

    def test_approved_false_rejected_for_submit(self) -> None:
        approval = self._approval(approved=False)

        with self.assertRaisesRegex(ExecutionValidationError, "approved=false"):
            validate_approval(approval, now=NOW, require_approved=True)

    def test_kill_switch_rejected_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kill = Path(temp_dir) / "KILL_SWITCH"
            kill.write_text("stop", encoding="utf-8")

            errors = approval_validation_errors(self._approval(), now=NOW, kill_switch_file=kill)

        self.assertIn("kill switch is active", errors)


if __name__ == "__main__":
    unittest.main()
