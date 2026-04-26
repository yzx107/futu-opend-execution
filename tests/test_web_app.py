from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.normal_trade import NormalTradeQuote
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.web_app import (
    REAL_CONFIRM_TEXT,
    WebState,
    api_health,
    api_normal_order,
    api_quote,
)


class FakeNormalTradeClient:
    place_count = 0

    def __init__(self, config) -> None:
        self.config = config

    def __enter__(self) -> "FakeNormalTradeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read_quote(self, symbol: str) -> NormalTradeQuote:
        return NormalTradeQuote(
            symbol=symbol,
            lot_size=100,
            best_bid="494.6",
            best_ask="494.8",
            last_price="494.7",
            raw_quote={"name": "腾讯控股"},
        )

    def place_order(self, intent):
        type(self).place_count += 1
        return [{"order_id": "1", "code": intent.symbol, "order_status": "SUBMITTED"}]

    def wait_for_terminal_order(self, *, order_id: str, symbol: str):
        return [[{"order_id": order_id, "code": symbol, "order_status": "FILLED_ALL"}]]


class FailingNormalTradeClient:
    def __init__(self, config) -> None:
        self.config = config

    def __enter__(self) -> "FailingNormalTradeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read_quote(self, symbol: str):
        del symbol
        raise RuntimeError("OpenD unreachable")


def make_state(temp_dir: str, *, allow_real_trade: bool = False) -> WebState:
    return WebState(
        config=RuntimeConfig(allow_real_trade=allow_real_trade),
        log_file=Path(temp_dir) / "web.jsonl",
        kill_switch_file=Path(temp_dir) / "KILL",
    )


class WebAppTests(unittest.TestCase):
    def test_health_reports_kill_switch_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            state.kill_switch_file.write_text("stop", encoding="utf-8")

            health = api_health(state)

        self.assertTrue(health["kill_switch"])
        self.assertFalse(health["allow_real_trade"])

    def test_active_health_probe_reports_quote_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)

            with patch(
                "futu_opend_execution.web_app.FutuNormalTradeClient",
                FakeNormalTradeClient,
            ):
                health = api_health(state, active=True, symbol="00700")

        self.assertEqual(health["status"], "READY")
        self.assertTrue(health["opend_quote_probe"]["ok"])
        self.assertEqual(health["opend_quote_probe"]["symbol"], "HK.00700")

    def test_active_health_probe_marks_degraded_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)

            with patch(
                "futu_opend_execution.web_app.FutuNormalTradeClient",
                FailingNormalTradeClient,
            ):
                health = api_health(state, active="true", symbol="00700")

        self.assertEqual(health["status"], "DEGRADED")
        self.assertFalse(health["opend_quote_probe"]["ok"])
        self.assertEqual(health["opend_quote_probe"]["error_type"], "RuntimeError")

    def test_quote_api_uses_client_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)

            with patch(
                "futu_opend_execution.web_app.FutuNormalTradeClient",
                FakeNormalTradeClient,
            ):
                payload = api_quote(state, symbol="00700")

        self.assertEqual(payload["quote"]["symbol"], "HK.00700")
        self.assertEqual(payload["quote"]["lot_size"], 100)
        self.assertEqual(payload["quote"]["name"], "腾讯控股")

    def test_normal_order_dry_run_does_not_place_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            FakeNormalTradeClient.place_count = 0
            state = make_state(temp_dir)

            with patch(
                "futu_opend_execution.web_app.FutuNormalTradeClient",
                FakeNormalTradeClient,
            ):
                payload = api_normal_order(
                    state,
                    {
                        "symbol": "00700",
                        "side": "BUY",
                        "order_type": "NORMAL",
                        "quantity_mode": "LOTS",
                        "lots": 1,
                        "limit_price": "495",
                        "max_notional": "50000",
                        "real": False,
                    },
                )

        self.assertFalse(payload["submitted"])
        self.assertEqual(payload["intent"]["quantity"], 100)
        self.assertEqual(FakeNormalTradeClient.place_count, 0)

    def test_real_order_requires_environment_and_confirm_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir, allow_real_trade=False)

            with self.assertRaises(ExecutionValidationError):
                api_normal_order(
                    state,
                    {
                        "symbol": "00700",
                        "side": "BUY",
                        "order_type": "MARKET",
                        "quantity_mode": "LOTS",
                        "lots": 1,
                        "max_notional": "50000",
                        "real": True,
                        "confirm_text": REAL_CONFIRM_TEXT,
                    },
                )

    def test_kill_switch_blocks_even_dry_run_order_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            state.kill_switch_file.write_text("stop", encoding="utf-8")

            with self.assertRaises(ExecutionValidationError):
                api_normal_order(
                    state,
                    {
                        "symbol": "00700",
                        "side": "BUY",
                        "order_type": "NORMAL",
                        "quantity_mode": "LOTS",
                        "lots": 1,
                        "limit_price": "495",
                        "max_notional": "50000",
                        "real": False,
                    },
                )

    def test_real_duplicate_click_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            FakeNormalTradeClient.place_count = 0
            state = make_state(temp_dir, allow_real_trade=True)
            payload = {
                "symbol": "00700",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity_mode": "LOTS",
                "lots": 1,
                "max_notional": "50000",
                "real": True,
                "confirm_text": REAL_CONFIRM_TEXT,
            }

            with patch(
                "futu_opend_execution.web_app.FutuNormalTradeClient",
                FakeNormalTradeClient,
            ):
                first = api_normal_order(state, payload)
                with self.assertRaises(ExecutionValidationError):
                    api_normal_order(state, payload)

        self.assertTrue(first["submitted"])
        self.assertEqual(FakeNormalTradeClient.place_count, 1)


if __name__ == "__main__":
    unittest.main()
