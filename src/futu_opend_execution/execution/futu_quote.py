"""Futu OpenD quote adapter."""

from __future__ import annotations

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.execution.market_data import (
    MarketDataResponseError,
)
from futu_opend_execution.models import MarketSession, OrderBookSnapshot, QuoteLevel
from futu_opend_execution.risk import validate_runtime_config


class FutuOpenDQuoteClient:
    """Market-data adapter backed by the optional `futu-api` Python SDK."""

    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self._config = config or RuntimeConfig.from_env()
        validate_runtime_config(self._config)
        self._futu = load_futu_module(self._config)
        self._quote_context = self._futu.OpenQuoteContext(
            host=self._config.futu_host,
            port=self._config.futu_port,
        )

    def __enter__(self) -> "FutuOpenDQuoteClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get_market_state(self, *, symbol: str) -> str:
        broker_symbol = self._normalize_symbol(symbol)
        ret, data = self._quote_context.get_market_state([broker_symbol])
        if ret != self._futu.RET_OK:
            raise MarketDataResponseError(f"get_market_state failed: {data}")
        if getattr(data, "shape", (0,))[0] <= 0:
            raise MarketDataResponseError("get_market_state returned no rows.")
        return str(data["market_state"][0]).strip().upper()

    def ensure_order_book_subscription(self, *, symbol: str, depth: int) -> None:
        del depth

        broker_symbol = self._normalize_symbol(symbol)
        ret, data = self._quote_context.subscribe(
            [broker_symbol],
            [self._futu.SubType.ORDER_BOOK],
            subscribe_push=False,
        )
        if ret != self._futu.RET_OK:
            raise MarketDataResponseError(f"subscribe ORDER_BOOK failed: {data}")

    def get_order_book_snapshot(self, *, symbol: str, depth: int) -> OrderBookSnapshot:
        broker_symbol = self._normalize_symbol(symbol)
        ret, data = self._quote_context.get_order_book(broker_symbol, num=depth)
        if ret != self._futu.RET_OK:
            raise MarketDataResponseError(f"get_order_book failed: {data}")

        asks = tuple(
            QuoteLevel(price=level[0], quantity=int(level[1]))
            for level in data.get("Ask", [])
        )
        bids = tuple(
            QuoteLevel(price=level[0], quantity=int(level[1]))
            for level in data.get("Bid", [])
        )
        return OrderBookSnapshot(
            symbol=self._strip_market_prefix(broker_symbol),
            asks=asks,
            bids=bids,
            session=MarketSession.GREY,
        )

    def close(self) -> None:
        self._quote_context.close()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if "." in normalized:
            return normalized
        return f"HK.{normalized}"

    @staticmethod
    def _strip_market_prefix(symbol: str) -> str:
        if "." not in symbol:
            return symbol
        _, code = symbol.split(".", 1)
        return code
