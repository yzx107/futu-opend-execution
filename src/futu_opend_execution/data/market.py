"""Unified market event/state models used by replay, live, and strategies."""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class MarketEvent:
    symbol: str
    timestamp: datetime
    event_type: str
    price: Decimal | str | int | float | None = None
    volume: Decimal | str | int | float = Decimal("0")
    turnover: Decimal | str | int | float | None = None
    side: str | None = None
    bid_price: Decimal | str | int | float | None = None
    bid_size: Decimal | str | int | float | None = None
    ask_price: Decimal | str | int | float | None = None
    ask_size: Decimal | str | int | float | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if "." not in symbol:
            symbol = f"HK.{symbol}"
        object.__setattr__(self, "symbol", symbol)
        for name in ("price", "volume", "turnover", "bid_price", "bid_size", "ask_price", "ask_size"):
            object.__setattr__(self, name, _to_decimal(getattr(self, name)))
        if self.side is not None:
            object.__setattr__(self, "side", self.side.strip().upper())


@dataclass(frozen=True, slots=True)
class MarketState:
    symbol: str
    timestamp: datetime
    interval_seconds: int
    last_price: Decimal | None
    best_bid: Decimal | None
    bid_size: Decimal
    best_ask: Decimal | None
    ask_size: Decimal
    spread_bps: Decimal
    orderbook_imbalance: Decimal
    opening_vwap: Decimal | None
    rolling_vwap: Decimal | None
    realized_vol: Decimal
    rolling_high: Decimal | None
    rolling_low: Decimal | None
    cumulative_volume: Decimal
    cumulative_turnover: Decimal
    volume_delta: Decimal
    turnover_delta: Decimal
    tick_count: int
    source: str = "unknown"
    previous_close: Decimal | None = None
    open_price: Decimal | None = None
    market_state: str | None = None
    stale: bool = False
    orderbook_limited: bool = False


def build_market_states(
    events: Iterable[MarketEvent],
    *,
    interval_seconds: int = 1,
    rolling_window: int = 30,
    source: str = "replay",
) -> list[MarketState]:
    ordered = sorted(events, key=lambda item: item.timestamp)
    if not ordered:
        return []

    states: list[MarketState] = []
    bucket_start = _floor_time(ordered[0].timestamp, interval_seconds)
    bucket_events: list[MarketEvent] = []
    prices: list[Decimal] = []
    vwap_inputs: list[tuple[Decimal, Decimal]] = []
    cumulative_volume = Decimal("0")
    cumulative_turnover = Decimal("0")
    last_price: Decimal | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    bid_size = Decimal("0")
    ask_size = Decimal("0")
    tick_count = 0

    def flush(until: datetime) -> None:
        nonlocal bucket_events, cumulative_volume, cumulative_turnover, last_price, best_bid, best_ask, bid_size, ask_size, tick_count
        if not bucket_events:
            return
        volume_delta = Decimal("0")
        turnover_delta = Decimal("0")
        for event in bucket_events:
            if event.event_type == "trade" and event.price is not None:
                last_price = event.price
                volume = event.volume or Decimal("0")
                turnover = event.turnover if event.turnover is not None else event.price * volume
                volume_delta += volume
                turnover_delta += turnover
                prices.append(event.price)
                vwap_inputs.append((event.price, volume))
                tick_count += 1
            best_bid, bid_size, best_ask, ask_size = _apply_book_event(event, best_bid, bid_size, best_ask, ask_size)
        cumulative_volume += volume_delta
        cumulative_turnover += turnover_delta
        window_prices = prices[-rolling_window:]
        window_vwap = vwap_inputs[-rolling_window:]
        states.append(
            MarketState(
                symbol=bucket_events[-1].symbol,
                timestamp=until,
                interval_seconds=interval_seconds,
                last_price=last_price,
                best_bid=best_bid,
                bid_size=bid_size,
                best_ask=best_ask,
                ask_size=ask_size,
                spread_bps=_spread_bps(best_bid, best_ask, last_price),
                orderbook_imbalance=_imbalance(bid_size, ask_size),
                opening_vwap=_vwap(vwap_inputs),
                rolling_vwap=_vwap(window_vwap),
                realized_vol=_realized_vol(window_prices),
                rolling_high=max(window_prices) if window_prices else None,
                rolling_low=min(window_prices) if window_prices else None,
                cumulative_volume=cumulative_volume,
                cumulative_turnover=cumulative_turnover,
                volume_delta=volume_delta,
                turnover_delta=turnover_delta,
                tick_count=tick_count,
                source=source,
                orderbook_limited=best_bid is None or best_ask is None,
            )
        )
        bucket_events = []

    for event in ordered:
        bucket = _floor_time(event.timestamp, interval_seconds)
        if bucket != bucket_start:
            flush(bucket_start + timedelta(seconds=interval_seconds))
            bucket_start = bucket
        bucket_events.append(event)
    flush(bucket_start + timedelta(seconds=interval_seconds))
    return states


def market_state_to_jsonable(state: MarketState) -> dict[str, Any]:
    payload = asdict(state)
    payload["timestamp"] = state.timestamp.isoformat()
    for key, value in list(payload.items()):
        if isinstance(value, Decimal):
            payload[key] = str(value)
    return payload


def _apply_book_event(
    event: MarketEvent,
    best_bid: Decimal | None,
    bid_size: Decimal,
    best_ask: Decimal | None,
    ask_size: Decimal,
) -> tuple[Decimal | None, Decimal, Decimal | None, Decimal]:
    if event.bid_price is not None:
        best_bid = event.bid_price
        bid_size = event.bid_size or Decimal("0")
    if event.ask_price is not None:
        best_ask = event.ask_price
        ask_size = event.ask_size or Decimal("0")
    if event.event_type == "order" and event.price is not None:
        if event.side in {"B", "BUY", "BID"} and (best_bid is None or event.price >= best_bid):
            best_bid, bid_size = event.price, event.volume or Decimal("0")
        if event.side in {"S", "SELL", "ASK"} and (best_ask is None or event.price <= best_ask):
            best_ask, ask_size = event.price, event.volume or Decimal("0")
    return best_bid, bid_size, best_ask, ask_size


def _floor_time(value: datetime, interval_seconds: int) -> datetime:
    seconds = max(interval_seconds, 1)
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=value.tzinfo)


def _to_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _vwap(inputs: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    volume = sum((item[1] for item in inputs), Decimal("0"))
    if volume <= 0:
        return None
    return sum((price * qty for price, qty in inputs), Decimal("0")) / volume


def _realized_vol(prices: list[Decimal]) -> Decimal:
    if len(prices) < 2:
        return Decimal("0")
    return Decimal(str(statistics.pstdev([float(price) for price in prices])))


def _spread_bps(best_bid: Decimal | None, best_ask: Decimal | None, last_price: Decimal | None) -> Decimal:
    if best_bid is None or best_ask is None:
        return Decimal("0")
    base = last_price or ((best_bid + best_ask) / Decimal("2"))
    if base <= 0:
        return Decimal("0")
    return max((best_ask - best_bid) / base * Decimal("10000"), Decimal("0"))


def _imbalance(bid_size: Decimal, ask_size: Decimal) -> Decimal:
    total = bid_size + ask_size
    if total <= 0:
        return Decimal("0")
    return (bid_size - ask_size) / total
