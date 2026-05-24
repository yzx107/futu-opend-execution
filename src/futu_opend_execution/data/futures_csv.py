"""CSV replay provider for futures research fixtures and handoffs."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable

from futu_opend_execution.data.market import MarketEvent, MarketState, build_market_states


class FuturesCsvReplayProvider:
    def __init__(
        self,
        *,
        path: Path | str,
        symbol: str,
        interval_seconds: int = 1,
        limit_rows: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.symbol = _normalize_symbol(symbol)
        self.interval_seconds = max(int(interval_seconds), 1)
        self.limit_rows = limit_rows

    def iter_events(self) -> Iterable[MarketEvent]:
        rows = 0
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                row_symbol = _normalize_symbol(row.get("symbol") or self.symbol)
                if row_symbol != self.symbol:
                    continue
                rows += 1
                if self.limit_rows is not None and rows > self.limit_rows:
                    break
                timestamp = _time(row.get("timestamp") or row.get("SendTime") or row.get("time"))
                price = row.get("price") or row.get("last_price") or row.get("TradePrice")
                volume = row.get("volume") or row.get("qty") or row.get("TradeVolume") or 0
                if price not in {None, ""}:
                    yield MarketEvent(symbol=self.symbol, timestamp=timestamp, event_type="trade", price=price, volume=volume)
                bid = row.get("bid_price") or row.get("best_bid") or row.get("BestBidReplay")
                ask = row.get("ask_price") or row.get("best_ask") or row.get("BestAskReplay")
                if bid not in {None, ""} or ask not in {None, ""}:
                    yield MarketEvent(
                        symbol=self.symbol,
                        timestamp=timestamp,
                        event_type="book",
                        bid_price=bid or None,
                        bid_size=row.get("bid_size") or row.get("BestBidSizeReplay") or 0,
                        ask_price=ask or None,
                        ask_size=row.get("ask_size") or row.get("BestAskSizeReplay") or 0,
                        book_quality=row.get("book_quality") or "OK",
                    )

    def iter_market_states(self) -> Iterable[MarketState]:
        yield from build_market_states(self.iter_events(), interval_seconds=self.interval_seconds, source="futures_csv")

    @classmethod
    def from_events(
        cls,
        events: Iterable[MarketEvent],
        *,
        interval_seconds: int = 1,
    ) -> "InMemoryFuturesReplayProvider":
        return InMemoryFuturesReplayProvider(events, interval_seconds=interval_seconds)


class InMemoryFuturesReplayProvider:
    def __init__(self, events: Iterable[MarketEvent], *, interval_seconds: int = 1) -> None:
        self.events = tuple(events)
        self.interval_seconds = interval_seconds

    def iter_events(self) -> Iterable[MarketEvent]:
        return iter(self.events)

    def iter_market_states(self) -> Iterable[MarketState]:
        yield from build_market_states(self.events, interval_seconds=self.interval_seconds, source="futures_fixture")


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        normalized = f"HK.{normalized}"
    return normalized


def _time(value: str | None) -> datetime:
    if not value:
        raise ValueError("timestamp is required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
