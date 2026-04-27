from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.normal_trade import NormalTradeQuote
from futu_opend_execution.risk import ExecutionValidationError
from futu_opend_execution.web_app import (
    REAL_CONFIRM_TEXT,
    STATIC_ROOT,
    WebState,
    api_approve_cost_reducer_intent,
    api_cost_reducer_config,
    api_inventory_seed_dry_run,
    api_health,
    api_kill_switch,
    api_normal_order,
    api_quote,
    api_replay_run,
    api_start_live_dry_run,
    api_start_live_real_buy_only,
    api_update_cost_reducer_config,
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


class FakeThread:
    instances: list["FakeThread"] = []

    def __init__(self, *, target, args, daemon) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        type(self).instances.append(self)

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.started


def make_state(temp_dir: str, *, allow_real_trade: bool = False) -> WebState:
    return WebState(
        config=RuntimeConfig(
            allow_real_trade=allow_real_trade,
            futu_trade_password="pw" if allow_real_trade else None,
        ),
        log_file=Path(temp_dir) / "web.jsonl",
        kill_switch_file=Path(temp_dir) / "KILL",
    )


class WebAppTests(unittest.TestCase):
    def _grey_real_payload(self) -> dict:
        return {
            "symbol": "HK.01234",
            "quantity": 1000,
            "max_price": "10",
            "max_qty": 1000,
            "max_notional": "10000",
            "max_order_attempts": 3,
            "cool_down_ms": 300,
            "lot_size": 100,
            "real": True,
            "real_mode": True,
            "acknowledge_real_order": True,
            "confirm_text": REAL_CONFIRM_TEXT,
            "timeout_seconds": 1,
            "poll_interval_ms": 50,
        }

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

    def test_live_dry_run_starts_background_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            payload = {**self._grey_real_payload(), "real": False, "real_mode": False}
            FakeThread.instances = []

            with patch("futu_opend_execution.web_app.Thread", FakeThread):
                result = api_start_live_dry_run(state, payload)

            self.assertTrue(result["running"])
            self.assertEqual(result["execution_mode"], "LIVE_DRY_RUN")
            self.assertEqual(result["symbol"], "HK.01234")
            self.assertEqual(len(FakeThread.instances), 1)
            self.assertTrue(FakeThread.instances[0].started)
            events = [
                json.loads(line)
                for line in state.log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(event["event"] == "web_live_dry_run_started" for event in events))

    def test_live_dry_run_rejects_active_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            state.kill_switch_file.write_text("stop", encoding="utf-8")
            payload = {**self._grey_real_payload(), "real": False, "real_mode": False}

            with self.assertRaises(ExecutionValidationError):
                api_start_live_dry_run(state, payload)

    def test_live_real_grey_buy_only_requires_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir, allow_real_trade=False)

            with self.assertRaises(ExecutionValidationError):
                api_start_live_real_buy_only(state, self._grey_real_payload())

            events = [
                json.loads(line)
                for line in state.log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(event["event"] == "blocked_real_order" for event in events))

    def test_live_real_grey_buy_only_requires_trade_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = WebState(
                config=RuntimeConfig(allow_real_trade=True, futu_trade_password=None),
                log_file=Path(temp_dir) / "web.jsonl",
                kill_switch_file=Path(temp_dir) / "KILL",
            )

            with self.assertRaises(ExecutionValidationError):
                api_start_live_real_buy_only(state, self._grey_real_payload())

            events = [
                json.loads(line)
                for line in state.log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(event["event"] == "blocked_real_order" for event in events))

    def test_live_real_grey_buy_only_requires_ack_and_phrase(self) -> None:
        cases = [
            {"acknowledge_real_order": False, "confirm_text": REAL_CONFIRM_TEXT},
            {"acknowledge_real_order": True, "confirm_text": "wrong"},
            {"acknowledge_real_order": True, "confirm_text": REAL_CONFIRM_TEXT, "real_mode": False},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                with tempfile.TemporaryDirectory() as temp_dir:
                    state = make_state(temp_dir, allow_real_trade=True)
                    payload = {**self._grey_real_payload(), **updates}

                    with self.assertRaises(ExecutionValidationError):
                        api_start_live_real_buy_only(state, payload)

                    events = [
                        json.loads(line)
                        for line in state.log_file.read_text(encoding="utf-8").splitlines()
                    ]
                    self.assertTrue(any(event["event"] == "blocked_real_order" for event in events))

    def test_live_real_grey_buy_only_rejects_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir, allow_real_trade=True)
            state.kill_switch_file.write_text("stop", encoding="utf-8")

            with self.assertRaises(ExecutionValidationError):
                api_start_live_real_buy_only(state, self._grey_real_payload())

    def test_live_real_grey_buy_only_starts_guarded_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir, allow_real_trade=True)
            FakeThread.instances = []

            with patch("futu_opend_execution.web_app.Thread", FakeThread):
                result = api_start_live_real_buy_only(state, self._grey_real_payload())

            self.assertTrue(result["running"])
            self.assertEqual(result["execution_mode"], "LIVE_REAL_BUY_ONLY")
            self.assertEqual(result["symbol"], "HK.01234")
            self.assertEqual(len(FakeThread.instances), 1)
            self.assertTrue(FakeThread.instances[0].started)
            self.assertTrue(FakeThread.instances[0].daemon)
            events = [
                json.loads(line)
                for line in state.log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(event["event"] == "web_grey_real_buy_armed" for event in events))

    def test_cost_reducer_config_endpoint_accepts_spread_and_vol_params(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            payload = api_update_cost_reducer_config(
                state,
                {
                    "max_spread_bps": "100",
                    "overextension_vol_multiple": "1.5",
                    "high_pullback_vol_multiple": "0.25",
                },
            )

        self.assertEqual(payload["config"]["max_spread_bps"], "100")
        self.assertEqual(payload["config"]["overextension_vol_multiple"], "1.5")
        self.assertEqual(api_cost_reducer_config(state)["config"]["high_pullback_vol_multiple"], "0.25")

    def test_cost_reducer_config_endpoint_validates_numeric_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            with self.assertRaises(ValueError):
                api_update_cost_reducer_config(state, {"max_sell_total_position_ratio": "2"})

    def test_approve_endpoint_rejects_without_real_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir, allow_real_trade=False)
            api_inventory_seed_dry_run(
                state,
                {
                    "target_quantity": 1000,
                    "lot_size": 100,
                    "anchor_price": "10",
                },
            )
            state.pending_cost_reducer_intents["i1"] = {
                "side": "SELL",
                "role": "TRADING_SELL",
                "quantity": 100,
                "limit_price": "10",
                "market_snapshot": {"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
            }
            with self.assertRaises(ExecutionValidationError):
                api_approve_cost_reducer_intent(
                    state,
                    {
                        "intent_id": "i1",
                        "real_mode": True,
                        "acknowledge_real_order": True,
                        "confirm_text": REAL_CONFIRM_TEXT,
                        "max_notional": "20000",
                        "lot_size": 100,
                        "best_bid": "10",
                    },
                )

    def test_approve_endpoint_rejects_without_confirmation_phrase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir, allow_real_trade=True)
            api_inventory_seed_dry_run(
                state,
                {
                    "target_quantity": 1000,
                    "lot_size": 100,
                    "anchor_price": "10",
                },
            )
            state.pending_cost_reducer_intents["i1"] = {
                "side": "SELL",
                "role": "TRADING_SELL",
                "quantity": 100,
                "limit_price": "10",
                "market_snapshot": {"spread_bps": "1", "max_spread_bps": "20", "best_bid": "10"},
            }
            with self.assertRaises(ExecutionValidationError):
                api_approve_cost_reducer_intent(
                    state,
                    {
                        "intent_id": "i1",
                        "real_mode": True,
                        "acknowledge_real_order": True,
                        "confirm_text": "wrong",
                        "max_notional": "20000",
                        "lot_size": 100,
                        "best_bid": "10",
                    },
                )

    def test_approve_endpoint_logs_blocked_real_order_for_fail_closed_paths(self) -> None:
        cases = [
            ("missing real mode", {"real_mode": False, "acknowledge_real_order": True, "confirm_text": REAL_CONFIRM_TEXT}),
            ("missing acknowledgement", {"real_mode": True, "acknowledge_real_order": False, "confirm_text": REAL_CONFIRM_TEXT}),
            ("missing confirmation phrase", {"real_mode": True, "acknowledge_real_order": True, "confirm_text": ""}),
            ("submit_real flag not set", {"real_mode": True, "acknowledge_real_order": True, "confirm_text": REAL_CONFIRM_TEXT}),
            ("kill switch", {"real_mode": True, "acknowledge_real_order": True, "confirm_text": REAL_CONFIRM_TEXT, "kill": True}),
            ("stale market", {"real_mode": True, "acknowledge_real_order": True, "confirm_text": REAL_CONFIRM_TEXT, "stale": True}),
            ("spread too wide", {"real_mode": True, "acknowledge_real_order": True, "confirm_text": REAL_CONFIRM_TEXT, "spread": "30"}),
        ]
        for _, case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temp_dir:
                    state = make_state(temp_dir, allow_real_trade=True)
                    api_inventory_seed_dry_run(
                        state,
                        {
                            "target_quantity": 1000,
                            "lot_size": 100,
                            "anchor_price": "10",
                        },
                    )
                    if case.get("kill"):
                        state.kill_switch_file.write_text("stop", encoding="utf-8")
                    state.pending_cost_reducer_intents["i1"] = {
                        "side": "SELL",
                        "role": "TRADING_SELL",
                        "quantity": 100,
                        "limit_price": "10",
                        "market_snapshot": {
                            "spread_bps": case.get("spread", "1"),
                            "max_spread_bps": "20",
                            "best_bid": "10",
                            "stale": bool(case.get("stale", False)),
                        },
                    }
                    payload = {
                        "intent_id": "i1",
                        "max_notional": "20000",
                        "lot_size": 100,
                        "best_bid": "10",
                        **case,
                    }
                    payload.pop("kill", None)
                    payload.pop("stale", None)
                    payload.pop("spread", None)
                    with self.assertRaises(ExecutionValidationError):
                        api_approve_cost_reducer_intent(state, payload)
                    events = [
                        json.loads(line)
                        for line in state.log_file.read_text(encoding="utf-8").splitlines()
                    ]
                    self.assertTrue(any(event["event"] == "blocked_real_order" for event in events))

    def test_replay_endpoint_emits_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            replay_path = temp_path / "events.jsonl"
            replay_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "symbol": "HK.01234",
                                "dark_status": "TRADING",
                                "best_bid": "10.00",
                                "best_ask": "10.02",
                                "raw_quote": {
                                    "dark_status": "TRADING",
                                    "last_price": "10.01",
                                    "turnover": "10000",
                                    "volume": "1000",
                                    "lot_size": 100,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "symbol": "HK.01234",
                                "dark_status": "TRADING",
                                "best_bid": "10.10",
                                "best_ask": "10.12",
                                "raw_quote": {
                                    "dark_status": "TRADING",
                                    "last_price": "10.11",
                                    "turnover": "12020",
                                    "volume": "1200",
                                    "lot_size": 100,
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state = make_state(temp_dir)
            result = api_replay_run(
                state,
                {
                    "input_path": str(replay_path),
                    "output_log_path": str(temp_path / "replay.jsonl"),
                    "symbol": "HK.01234",
                    "quantity": 1000,
                    "max_price": "12.80",
                    "max_qty": 1000,
                    "max_notional": "12800",
                    "cost_reducer_dry_run": True,
                },
            )

        self.assertIsNotNone(result["summary"])
        self.assertEqual(result["summary"]["event"], "cost_reducer_replay_summary")

    def test_kill_switch_endpoint_blocks_order_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = make_state(temp_dir)
            api_kill_switch(state, {"enabled": True})
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

    def test_web_ui_smoke_contains_real_trade_console_sections(self) -> None:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        for token in (
            "costReducerSection",
            "inventorySection",
            "ordersFillsSection",
            "costReducerConfirmText",
            "greyConfirmText",
            "greyAckReal",
            "realModeStatus",
            "maxSpreadBps",
            "replaySection",
            "实盘暗盘抢单",
            "50/50 持仓",
        ):
            self.assertIn(token, html)


if __name__ == "__main__":
    unittest.main()
