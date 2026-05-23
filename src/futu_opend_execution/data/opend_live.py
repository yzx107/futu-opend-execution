"""Read-only OpenD live quote provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from futu_opend_execution.config import RuntimeConfig, harden_local_opend_environment
from futu_opend_execution.data.market import MarketEvent, MarketState, build_market_states
from futu_opend_execution.execution.futu_runtime import load_futu_module


@dataclass(slots=True)
class OpenDLiveProvider:
    symbol: str
    config: RuntimeConfig | None = None
    order_book_depth: int = 10

    def __post_init__(self) -> None:
        harden_local_opend_environment()
        self.config = self.config or RuntimeConfig.from_env()
        self.symbol = _normalize_symbol(self.symbol)
        self._futu = load_futu_module(self.config)
        self._quote_ctx = self._futu.OpenQuoteContext(host=self.config.futu_host, port=self.config.futu_port)
        self._subscribed = False

    def __enter__(self) -> "OpenDLiveProvider":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        close = getattr(self._quote_ctx, "close", None)
        if callable(close):
            close()

    def read_once(self) -> MarketState:
        self._subscribe()
        now = datetime.now().astimezone()
        quote = self._snapshot()
        book = self._order_book()
        last_price = _first_decimal(quote, ("last_price", "cur_price", "close"))
        best_bid, bid_size, best_ask, ask_size = _book_summary(book)
        events = [
            MarketEvent(
                symbol=self.symbol,
                timestamp=now,
                event_type="trade",
                price=last_price,
                volume=0,
            ),
            MarketEvent(
                symbol=self.symbol,
                timestamp=now,
                event_type="book",
                bid_price=best_bid,
                bid_size=bid_size,
                ask_price=best_ask,
                ask_size=ask_size,
            ),
        ]
        return build_market_states(events, interval_seconds=1, source="opend_live")[-1]

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        subtypes = [
            getattr(self._futu.SubType, "QUOTE", None),
            getattr(self._futu.SubType, "ORDER_BOOK", None),
        ]
        subtypes = [item for item in subtypes if item is not None]
        if subtypes:
            ret, data = self._quote_ctx.subscribe([self.symbol], subtypes, subscribe_push=False)
            if ret != self._futu.RET_OK:
                raise RuntimeError(f"OpenD subscribe failed: {data}")
        self._subscribed = True

    def _snapshot(self) -> Any:
        ret, data = self._quote_ctx.get_market_snapshot([self.symbol])
        if ret != self._futu.RET_OK:
            raise RuntimeError(f"OpenD market snapshot failed: {data}")
        return data

    def _order_book(self) -> Any:
        ret, data = self._quote_ctx.get_order_book(self.symbol, num=self.order_book_depth)
        if ret != self._futu.RET_OK:
            raise RuntimeError(f"OpenD order book failed: {data}")
        return data


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        normalized = f"HK.{normalized}"
    return normalized


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        return [dict(item) for item in to_dict("records")]
    return []


def _first_decimal(data: Any, names: tuple[str, ...]) -> Decimal | None:
    for row in _records(data):
        for name in names:
            if row.get(name) not in {None, ""}:
                return Decimal(str(row[name]))
    return None


def _book_summary(book: Any) -> tuple[Decimal | None, Decimal, Decimal | None, Decimal]:
    if not isinstance(book, dict):
        return None, Decimal("0"), None, Decimal("0")
    bid_rows = book.get("Bid") or book.get("bid") or []
    ask_rows = book.get("Ask") or book.get("ask") or []
    best_bid = _row_decimal(bid_rows, 0, "price")
    bid_size = _sum_qty(bid_rows)
    best_ask = _row_decimal(ask_rows, 0, "price")
    ask_size = _sum_qty(ask_rows)
    return best_bid, bid_size, best_ask, ask_size


def _row_decimal(rows: list[Any], index: int, key: str) -> Decimal | None:
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        value = row.get(key)
    elif isinstance(row, (list, tuple)) and len(row) > index:
        value = row[index]
    else:
        value = None
    return None if value in {None, ""} else Decimal(str(value))


def _sum_qty(rows: list[Any]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        value = row.get("volume") or row.get("qty") if isinstance(row, dict) else (row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else 0)
        if value not in {None, ""}:
            total += Decimal(str(value))
    return total
