from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.grey_open import FutuGreyMarketOpenDClient
from futu_opend_execution.services.real_order import (
    GreyMarketRealOrderIntent,
    GreyOrderRole,
    GreyOrderSide,
    GreyOrderSource,
)


class FakeTradeContext:
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def unlock_trade(self, password):
        self.password = password
        return FakeFutu.RET_OK, {}

    def place_order(self, **kwargs):
        type(self).calls.append(kwargs)
        return FakeFutu.RET_OK, [{"order_id": "1"}]

    def close(self) -> None:
        return None


class FakeQuoteContext:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def close(self) -> None:
        return None


class FakeFutu:
    RET_OK = 0

    class TrdMarket:
        HK = "HK"

    class TrdSide:
        BUY = "BUY"
        SELL = "SELL"

    class OrderType:
        NORMAL = "NORMAL"
        MARKET = "MARKET"

    class TrdEnv:
        REAL = "REAL"

    class TimeInForce:
        DAY = "DAY"

    class SecurityFirm:
        FUTUSECURITIES = "FUTUSECURITIES"

    OpenQuoteContext = FakeQuoteContext
    OpenSecTradeContext = FakeTradeContext


class GreyOpenRealOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeTradeContext.calls = []

    def _client(self) -> FutuGreyMarketOpenDClient:
        return FutuGreyMarketOpenDClient(
            RuntimeConfig(
                allow_real_trade=True,
                futu_trade_password="pw",
            )
        )

    def _intent(self, *, side: GreyOrderSide, role: GreyOrderRole):
        return GreyMarketRealOrderIntent(
            symbol="HK.01234",
            side=side,
            quantity=100,
            limit_price=Decimal("10"),
            role=role,
            source=GreyOrderSource.COST_REDUCER,
            remark="test",
        )

    def test_place_real_limit_order_maps_buy_to_futu_buy_normal_real_day(self) -> None:
        with patch("futu_opend_execution.grey_open.load_futu_module", return_value=FakeFutu):
            with self._client() as client:
                client.place_real_limit_order(
                    self._intent(side=GreyOrderSide.BUY, role=GreyOrderRole.TRADING_REBUY)
                )

        call = FakeTradeContext.calls[-1]
        self.assertEqual(call["trd_side"], FakeFutu.TrdSide.BUY)
        self.assertEqual(call["order_type"], FakeFutu.OrderType.NORMAL)
        self.assertEqual(call["trd_env"], FakeFutu.TrdEnv.REAL)
        self.assertEqual(call["time_in_force"], FakeFutu.TimeInForce.DAY)

    def test_place_real_limit_order_maps_sell_to_futu_sell_normal_real_day(self) -> None:
        with patch("futu_opend_execution.grey_open.load_futu_module", return_value=FakeFutu):
            with self._client() as client:
                client.place_real_limit_order(
                    self._intent(side=GreyOrderSide.SELL, role=GreyOrderRole.TRADING_SELL)
                )

        call = FakeTradeContext.calls[-1]
        self.assertEqual(call["trd_side"], FakeFutu.TrdSide.SELL)
        self.assertEqual(call["order_type"], FakeFutu.OrderType.NORMAL)
        self.assertEqual(call["trd_env"], FakeFutu.TrdEnv.REAL)
        self.assertEqual(call["time_in_force"], FakeFutu.TimeInForce.DAY)


if __name__ == "__main__":
    unittest.main()
