from __future__ import annotations

import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import BrokerConfigurationError
from futu_opend_execution.normal_trade import (
    FutuNormalTradeClient,
    NormalOrderType,
    NormalQuantityMode,
    NormalTradeQuote,
    NormalTradeSide,
    build_normal_trade_intent,
    build_one_lot_intent,
    run_normal_trade,
)
from futu_opend_execution.risk import ExecutionValidationError


class FakeNormalTradeClient:
    def __init__(self, config) -> None:
        self.config = config
        self.placed = False

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
            last_price="494.8",
        )

    def place_limit_order(self, intent):
        self.placed = True
        return [{"order_id": "1", "code": intent.symbol}]

    def place_order(self, intent):
        self.placed = True
        return [{"order_id": "1", "code": intent.symbol}]

    def wait_for_terminal_order(self, *, order_id: str, symbol: str):
        return [[{"order_id": order_id, "code": symbol, "order_status": "FILLED_ALL"}]]


class NormalTradeTests(unittest.TestCase):
    def test_simulate_account_can_be_configured_from_env(self) -> None:
        config = RuntimeConfig.from_env({"FUTU_SIM_ACC_ID": "92000002"})

        self.assertEqual(config.futu_sim_acc_id, 92000002)

    def test_resolve_simulate_account_uses_configured_sim_acc_id(self) -> None:
        client = FutuNormalTradeClient.__new__(FutuNormalTradeClient)
        client._config = RuntimeConfig(futu_acc_id=1, futu_sim_acc_id=92000002)

        self.assertEqual(client._resolve_acc_id("SIMULATE"), 92000002)
        self.assertEqual(client._resolve_acc_id("REAL"), 1)

    def test_resolve_simulate_account_selects_hk_stock_sim_account(self) -> None:
        client = FutuNormalTradeClient.__new__(FutuNormalTradeClient)
        client._config = RuntimeConfig(futu_acc_id=9100000000000001)
        client.list_accounts = lambda: [
            {
                "acc_id": 9100000000000001,
                "trd_env": "REAL",
                "trdmarket_auth": ["HK"],
            },
            {
                "acc_id": 92000003,
                "trd_env": "SIMULATE",
                "sim_acc_type": "OPTION",
                "trdmarket_auth": ["HK"],
            },
            {
                "acc_id": 92000002,
                "trd_env": "SIMULATE",
                "sim_acc_type": "STOCK",
                "trdmarket_auth": ["HK"],
            },
        ]

        self.assertEqual(client._resolve_acc_id("SIMULATE"), 92000002)

    def test_resolve_simulate_account_fails_closed_without_stock_account(self) -> None:
        client = FutuNormalTradeClient.__new__(FutuNormalTradeClient)
        client._config = RuntimeConfig(futu_acc_id=9100000000000001)
        client.list_accounts = lambda: [{"acc_id": 1, "trd_env": "REAL"}]

        with self.assertRaises(BrokerConfigurationError):
            client._resolve_acc_id("SIMULATE")

    def test_build_one_lot_intent_uses_quote_lot_size(self) -> None:
        quote = NormalTradeQuote(
            symbol="00700",
            lot_size=100,
            best_bid="494.6",
            best_ask="494.8",
            last_price="494.8",
        )

        intent = build_one_lot_intent(
            quote=quote,
            side=NormalTradeSide.BUY,
            limit_price="495",
            max_notional="50000",
        )

        self.assertEqual(intent.symbol, "HK.00700")
        self.assertEqual(intent.quantity, 100)
        self.assertEqual(intent.order_type, NormalOrderType.NORMAL)
        self.assertEqual(intent.notional, Decimal("49500"))

    def test_market_order_uses_best_ask_as_buy_risk_price(self) -> None:
        quote = NormalTradeQuote(
            symbol="00700",
            lot_size=100,
            best_bid="494.6",
            best_ask="494.8",
            last_price="494.7",
        )

        intent = build_normal_trade_intent(
            quote=quote,
            side=NormalTradeSide.BUY,
            order_type=NormalOrderType.MARKET,
            quantity_mode=NormalQuantityMode.LOTS,
            lots=2,
            shares=None,
            limit_price=None,
            max_notional="100000",
        )

        self.assertEqual(intent.quantity, 200)
        self.assertEqual(intent.broker_price, Decimal("0"))
        self.assertEqual(intent.notional, Decimal("98960.0"))

    def test_share_quantity_mode_uses_raw_shares(self) -> None:
        quote = NormalTradeQuote(
            symbol="00700",
            lot_size=100,
            best_bid="494.6",
            best_ask="494.8",
            last_price="494.7",
        )

        intent = build_normal_trade_intent(
            quote=quote,
            side=NormalTradeSide.SELL,
            order_type=NormalOrderType.NORMAL,
            quantity_mode=NormalQuantityMode.SHARES,
            lots=None,
            shares=300,
            limit_price="494",
            max_notional="200000",
        )

        self.assertEqual(intent.quantity, 300)
        self.assertEqual(intent.notional, Decimal("148200"))

    def test_intent_rejects_notional_over_cap(self) -> None:
        quote = NormalTradeQuote(
            symbol="00700",
            lot_size=100,
            best_bid="494.6",
            best_ask="494.8",
            last_price="494.8",
        )

        with self.assertRaises(ExecutionValidationError):
            build_one_lot_intent(
                quote=quote,
                side=NormalTradeSide.BUY,
                limit_price="495",
                max_notional="100",
            )

    def test_dry_run_prints_intent_without_placing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            log_file = Path(temp_dir) / "normal.jsonl"

            with patch(
                "futu_opend_execution.normal_trade.FutuNormalTradeClient",
                FakeNormalTradeClient,
            ):
                rc = run_normal_trade(
                    symbol="00700",
                    side=NormalTradeSide.BUY,
                    limit_price="495",
                    max_notional="50000",
                    real=False,
                    log_file=log_file,
                    remark="test",
                    stdout=stdout,
                )

            records = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(rc, 0)
        self.assertIn("would_place_order", stdout.getvalue())
        self.assertTrue(
            any(
                record["event"] == "normal_trade_order_request"
                and record["dry_run"] is True
                for record in records
            )
        )


if __name__ == "__main__":
    unittest.main()
