from __future__ import annotations

import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution import (
    GreyMarketOpenTrigger,
    GreyMarketSignal,
    GreyMarketTriggerRules,
    JsonlEventLogger,
    RuntimeConfig,
    TriggerAction,
    run_replay,
    signal_from_record,
)
from futu_opend_execution.grey_open import run_live
from futu_opend_execution.risk import ExecutionValidationError


class GreyMarketOpenTriggerTests(unittest.TestCase):
    def test_generates_order_only_when_dark_trading_and_ask_within_cap(self) -> None:
        rules = GreyMarketTriggerRules(
            symbol="HK.01234",
            quantity=1000,
            max_price="12.80",
            max_qty=1000,
            max_notional="12800",
        )
        trigger = GreyMarketOpenTrigger(rules)

        wait_decision = trigger.evaluate(
            GreyMarketSignal(
                symbol="HK.01234",
                dark_status="NONE",
                best_ask="12.70",
            ),
            now_monotonic=0.0,
        )
        order_decision = trigger.evaluate(
            GreyMarketSignal(
                symbol="HK.01234",
                dark_status="TRADING",
                best_bid="12.60",
                best_ask="12.70",
                ask_quantity=1000,
            ),
            now_monotonic=1.0,
        )

        self.assertEqual(wait_decision.action, TriggerAction.WAIT)
        self.assertEqual(order_decision.action, TriggerAction.ORDER)
        self.assertIsNotNone(order_decision.intent)
        assert order_decision.intent is not None
        self.assertEqual(order_decision.intent.symbol, "HK.01234")
        self.assertEqual(order_decision.intent.limit_price, Decimal("12.80"))
        self.assertEqual(order_decision.intent.notional, Decimal("12800.00"))

    def test_kill_switch_blocks_order_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kill_switch = Path(temp_dir) / "STOP"
            kill_switch.write_text("stop", encoding="utf-8")
            rules = GreyMarketTriggerRules(
                symbol="01234",
                quantity=100,
                max_price="10",
                max_qty=100,
                max_notional="1000",
                kill_switch_file=kill_switch,
            )
            trigger = GreyMarketOpenTrigger(rules)

            decision = trigger.evaluate(
                GreyMarketSignal(
                    symbol="HK.01234",
                    dark_status="TRADING",
                    best_ask="9.90",
                ),
                now_monotonic=0.0,
            )

        self.assertEqual(decision.action, TriggerAction.BLOCK)
        self.assertIn("kill switch", decision.reason)

    def test_records_attempts_and_enforces_cooldown(self) -> None:
        rules = GreyMarketTriggerRules(
            symbol="01234",
            quantity=100,
            max_price="10",
            max_qty=100,
            max_notional="1000",
            cool_down_ms=300,
        )
        trigger = GreyMarketOpenTrigger(rules)
        signal = GreyMarketSignal(
            symbol="HK.01234",
            dark_status="TRADING",
            best_ask="9.90",
        )

        first = trigger.evaluate(signal, now_monotonic=0.0)
        trigger.record_attempt(now_monotonic=0.0)
        second = trigger.evaluate(signal, now_monotonic=0.1)
        third = trigger.evaluate(signal, now_monotonic=0.31)

        self.assertEqual(first.action, TriggerAction.ORDER)
        self.assertEqual(second.action, TriggerAction.WAIT)
        self.assertEqual(third.action, TriggerAction.ORDER)

    def test_opening_burst_uses_shorter_cooldown_for_first_second(self) -> None:
        rules = GreyMarketTriggerRules(
            symbol="01234",
            quantity=100,
            max_price="10",
            max_qty=100,
            max_notional="1000",
            max_order_attempts=5,
            cool_down_ms=300,
            opening_burst_seconds=1.0,
            opening_burst_cool_down_ms=50,
        )
        trigger = GreyMarketOpenTrigger(rules)
        signal = GreyMarketSignal(
            symbol="HK.01234",
            dark_status="TRADING",
            best_ask="9.90",
        )

        first = trigger.evaluate(signal, now_monotonic=0.0)
        trigger.record_attempt(now_monotonic=0.0)
        second = trigger.evaluate(signal, now_monotonic=0.08)
        trigger.record_attempt(now_monotonic=0.08)
        third = trigger.evaluate(signal, now_monotonic=1.20)
        trigger.record_attempt(now_monotonic=1.20)
        fourth = trigger.evaluate(signal, now_monotonic=1.35)

        self.assertEqual(first.action, TriggerAction.ORDER)
        self.assertEqual(second.action, TriggerAction.ORDER)
        self.assertEqual(third.action, TriggerAction.ORDER)
        self.assertEqual(fourth.action, TriggerAction.WAIT)

    def test_rule_validation_rejects_notional_over_cap(self) -> None:
        with self.assertRaises(ExecutionValidationError):
            GreyMarketTriggerRules(
                symbol="01234",
                quantity=101,
                max_price="10",
                max_qty=101,
                max_notional="1000",
            )

    def test_run_live_uses_push_signal_when_available(self) -> None:
        class FakeClient:
            def __init__(self, config) -> None:
                del config
                self.supports_push_signals = True
                self.poll_reads = 0
                self.push_reads = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def subscribe_market(self, symbol: str) -> None:
                del symbol

            def ensure_trade_context(self, logger) -> None:
                del logger

            def unlock_trade(self, logger) -> None:
                del logger

            def wait_for_push_signal(self, symbol: str, *, timeout_seconds, sleep, monotonic):
                del symbol, timeout_seconds, sleep, monotonic
                self.push_reads += 1
                return GreyMarketSignal(
                    symbol="HK.01234",
                    dark_status="TRADING",
                    best_ask="9.90",
                )

            def read_signal(self, symbol: str):
                del symbol
                self.poll_reads += 1
                raise AssertionError("read_signal should not be called when push signal is available")

        rules = GreyMarketTriggerRules(
            symbol="01234",
            quantity=100,
            max_price="10",
            max_qty=100,
            max_notional="1000",
            max_order_attempts=1,
            cool_down_ms=300,
        )
        logger = JsonlEventLogger(stream=io.StringIO())

        with patch("futu_opend_execution.grey_open.FutuGreyMarketOpenDClient", FakeClient):
            submitted = run_live(
                rules=rules,
                logger=logger,
                real=False,
                timeout_seconds=0.2,
                poll_interval_ms=50,
                config=RuntimeConfig(),
                stdout=io.StringIO(),
            )

        self.assertEqual(submitted, 1)


class GreyMarketReplayTests(unittest.TestCase):
    def test_signal_from_record_accepts_open_d_shaped_payload(self) -> None:
        signal = signal_from_record(
            {
                "event": "quote_event",
                "symbol": "01234",
                "raw_quote": {"dark_status": "TRADING"},
                "raw_order_book": {
                    "Ask": [["12.70", 1000, 1]],
                    "Bid": [["12.60", 500, 1]],
                    "svr_recv_time_ask": "2026-04-24 16:15:00.001",
                },
            },
            default_symbol="HK.01234",
        )

        self.assertEqual(signal.symbol, "HK.01234")
        self.assertEqual(signal.dark_status, "TRADING")
        self.assertEqual(signal.best_ask, Decimal("12.70"))
        self.assertEqual(signal.ask_quantity, 1000)
        self.assertEqual(signal.orderbook_timestamp, "2026-04-24 16:15:00.001")

    def test_replay_writes_dry_run_order_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            replay_path = temp_path / "events.jsonl"
            log_path = temp_path / "out.jsonl"
            replay_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "symbol": "HK.01234",
                                "dark_status": "NONE",
                                "best_ask": "12.70",
                            }
                        ),
                        json.dumps(
                            {
                                "symbol": "HK.01234",
                                "dark_status": "TRADING",
                                "best_ask": "12.70",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            rules = GreyMarketTriggerRules(
                symbol="HK.01234",
                quantity=1000,
                max_price="12.80",
                max_qty=1000,
                max_notional="12800",
            )
            stdout = io.StringIO()

            with JsonlEventLogger(log_path) as logger:
                submitted = run_replay(
                    input_path=replay_path,
                    rules=rules,
                    logger=logger,
                    stdout=stdout,
                )

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(submitted, 1)
        self.assertIn("would_place_order", stdout.getvalue())
        self.assertTrue(
            any(
                record["event"] == "order_request" and record["dry_run"] is True
                for record in records
            )
        )

    def test_replay_merges_split_quote_and_orderbook_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            replay_path = temp_path / "events.jsonl"
            log_path = temp_path / "out.jsonl"
            replay_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event": "quote_event",
                                "symbol": "HK.01234",
                                "dark_status": "TRADING",
                            }
                        ),
                        json.dumps(
                            {
                                "event": "orderbook_event",
                                "symbol": "HK.01234",
                                "best_bid": "12.60",
                                "best_ask": "12.70",
                            }
                        ),
                        json.dumps(
                            {
                                "event": "order_request",
                                "dry_run": True,
                                "intent": {"symbol": "HK.01234"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            rules = GreyMarketTriggerRules(
                symbol="HK.01234",
                quantity=1000,
                max_price="12.80",
                max_qty=1000,
                max_notional="12800",
            )
            stdout = io.StringIO()

            with JsonlEventLogger(log_path) as logger:
                submitted = run_replay(
                    input_path=replay_path,
                    rules=rules,
                    logger=logger,
                    stdout=stdout,
                )

        self.assertEqual(submitted, 1)
        self.assertIn("would_place_order", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
