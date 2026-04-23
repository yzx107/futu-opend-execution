"""Market-data interfaces and exceptions."""

from __future__ import annotations

from typing import Protocol

from futu_opend_execution.models import OrderBookSnapshot


class MarketDataError(RuntimeError):
    """Base class for market-data failures."""


class MarketDataResponseError(MarketDataError):
    """Raised when the quote provider returns an error or malformed payload."""


class MarketDataTimeoutError(MarketDataError):
    """Raised when waiting for a tradable market state times out."""


class MarketDataClient(Protocol):
    """Minimal market-data interface required by the grey-market harness."""

    def get_market_state(self, *, symbol: str) -> str:
        """Return the current market state string for the symbol."""

    def ensure_order_book_subscription(self, *, symbol: str, depth: int) -> None:
        """Ensure order-book data is available for subsequent snapshot queries."""

    def get_order_book_snapshot(self, *, symbol: str, depth: int) -> OrderBookSnapshot:
        """Fetch the current visible order book for a symbol."""

    def close(self) -> None:
        """Release any resources held by the market-data client."""
