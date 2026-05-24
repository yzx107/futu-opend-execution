from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from futu_opend_execution.cli.main import main
from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.futu_futures import FutuOpenDFuturesClient


class _FakeSecurityFirm:
    FUTUSECURITIES = "FUTUSECURITIES"


class _FakeQuoteContext:
    closed = False
    requested_codes = None

    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def get_future_info(self, codes):
        _FakeQuoteContext.requested_codes = list(codes)
        return 0, [
            {
                "code": "HK.HSI2606",
                "name": "Hang Seng Index Futures",
                "owner": "HSI",
                "exchange": "HKFE",
                "type": "股指期货",
                "size": "50",
                "size_unit": "index point",
                "price_currency": "HKD",
                "price_unit": "point",
                "min_change": "1",
                "min_change_unit": "point",
                "trade_time": "09:15-12:00,13:00-16:30",
                "time_zone": "Asia/Hong_Kong",
                "last_trade_time": "2026-06-29",
                "exchange_format_url": "https://www.hkex.com.hk/",
                "origin_code": "HSI",
            }
        ]

    def close(self) -> None:
        _FakeQuoteContext.closed = True


class _FakeFutureTradeContext:
    closed = False

    def __init__(self, *, host: str, port: int, security_firm) -> None:
        self.host = host
        self.port = port
        self.security_firm = security_firm

    def close(self) -> None:
        _FakeFutureTradeContext.closed = True


class _FakeFutuModule:
    RET_OK = 0
    SecurityFirm = _FakeSecurityFirm
    OpenQuoteContext = _FakeQuoteContext
    OpenFutureTradeContext = _FakeFutureTradeContext


class FutuFuturesClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeQuoteContext.closed = False
        _FakeQuoteContext.requested_codes = None
        _FakeFutureTradeContext.closed = False

    def test_get_future_info_maps_to_contract_spec(self) -> None:
        with patch("futu_opend_execution.execution.futu_futures._ensure_opend_socket"):
            with patch("futu_opend_execution.execution.futu_futures.load_futu_module", return_value=_FakeFutuModule):
                client = FutuOpenDFuturesClient(RuntimeConfig())
                infos = client.get_future_info(["HSI2606"])
                probe = client.probe_future_trade_context()
                client.close()

        self.assertEqual(_FakeQuoteContext.requested_codes, ["HK.HSI2606"])
        self.assertTrue(_FakeQuoteContext.closed)
        self.assertTrue(_FakeFutureTradeContext.closed)
        self.assertTrue(probe["ok"])
        self.assertEqual(infos[0].code, "HK.HSI2606")
        self.assertEqual(infos[0].contract_size, Decimal("50"))
        spec = infos[0].to_contract_spec(margin_rate="0.08", commission_per_contract="12")
        self.assertEqual(spec.symbol, "HK.HSI2606")
        self.assertEqual(spec.contract_multiplier, Decimal("50"))
        self.assertEqual(spec.tick_size, Decimal("1"))

    def test_cli_opend_info_uses_read_only_client(self) -> None:
        buffer = io.StringIO()
        with patch("futu_opend_execution.cli.main.FutuOpenDFuturesClient", _FakeCliFuturesClient):
            with redirect_stdout(buffer):
                code = main([
                    "futures",
                    "opend-info",
                    "HK.HSI2606",
                    "--check-trade-context",
                    "--margin-rate",
                    "0.08",
                    "--commission-per-contract",
                    "12",
                ])
        payload = json.loads(buffer.getvalue())

        self.assertEqual(code, 0)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["symbol_count"], 1)
        self.assertEqual(payload["contract_specs"][0]["symbol"], "HK.HSI2606")


class _FakeCliFuturesClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get_future_info(self, symbols):
        del symbols
        with patch("futu_opend_execution.execution.futu_futures._ensure_opend_socket"):
            with patch("futu_opend_execution.execution.futu_futures.load_futu_module", return_value=_FakeFutuModule):
                client = FutuOpenDFuturesClient(RuntimeConfig())
                try:
                    return client.get_future_info(["HK.HSI2606"])
                finally:
                    client.close()

    def probe_future_trade_context(self):
        return {"ok": True, "context": "OpenFutureTradeContext", "read_only": True}


if __name__ == "__main__":
    unittest.main()
