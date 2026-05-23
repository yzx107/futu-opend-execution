"""Stateful read-only OpenD live quote provider."""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from futu_opend_execution.config import RuntimeConfig, harden_local_opend_environment, is_local_opend_host
from futu_opend_execution.data.market import MarketState
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.watchlist import normalize_hk_symbol


@dataclass(slots=True)
class _RollingSample:
    timestamp: datetime
    last_price: Decimal | None
    volume_delta: Decimal
    turnover_delta: Decimal


@dataclass(slots=True)
class OpenDLiveProvider:
    symbols: str | Iterable[str]
    config: RuntimeConfig | None = None
    order_book_depth: int = 10
    rolling_window: int = 30
    stale_after_seconds: Decimal = Decimal("3")
    _history: dict[str, deque[_RollingSample]] = field(init=False, default_factory=dict)
    _last_cumulative: dict[str, tuple[Decimal, Decimal]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        harden_local_opend_environment()
        self.config = self.config or RuntimeConfig.from_env()
        if not is_local_opend_host(self.config.futu_host):
            raise ValueError("OpenD live provider requires a loopback host")
        if isinstance(self.symbols, str):
            normalized = (normalize_hk_symbol(self.symbols),)
        else:
            normalized = tuple(normalize_hk_symbol(symbol) for symbol in self.symbols)
        if not normalized:
            raise ValueError("OpenD live provider requires at least one symbol")
        self.symbols = normalized
        self._history = {symbol: deque(maxlen=self.rolling_window) for symbol in normalized}
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

    def read_once(self, symbol: str | None = None) -> MarketState:
        target = normalize_hk_symbol(symbol) if symbol else tuple(self.symbols)[0]
        return self.read_all(symbols=[target])[target]

    def read_all(self, symbols: Iterable[str] | None = None) -> dict[str, MarketState]:
        self._subscribe()
        targets = tuple(normalize_hk_symbol(symbol) for symbol in (symbols or self.symbols))
        snapshots = self._snapshots(targets)
        states: dict[str, MarketState] = {}
        for symbol in targets:
            snapshot = snapshots.get(symbol, {})
            book = self._order_book(symbol)
            states[symbol] = self._state_from_payload(symbol=symbol, snapshot=snapshot, book=book)
        return states

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        subtypes = [
            getattr(self._futu.SubType, "QUOTE", None),
            getattr(self._futu.SubType, "ORDER_BOOK", None),
        ]
        subtypes = [item for item in subtypes if item is not None]
        if subtypes:
            ret, data = self._quote_ctx.subscribe(list(self.symbols), subtypes, subscribe_push=False)
            if ret != self._futu.RET_OK:
                raise RuntimeError(f"OpenD subscribe failed: {data}")
        self._subscribed = True

    def _snapshots(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        ret, data = self._quote_ctx.get_market_snapshot(list(symbols))
        if ret != self._futu.RET_OK:
            raise RuntimeError(f"OpenD market snapshot failed: {data}")
        rows = _records(data)
        return {str(row.get("code", "")).upper(): row for row in rows if row.get("code")}

    def _order_book(self, symbol: str) -> dict[str, Any]:
        ret, data = self._quote_ctx.get_order_book(symbol, num=self.order_book_depth)
        if ret != self._futu.RET_OK:
            raise RuntimeError(f"OpenD order book failed for {symbol}: {data}")
        return data if isinstance(data, dict) else {}

    def _state_from_payload(self, *, symbol: str, snapshot: dict[str, Any], book: dict[str, Any]) -> MarketState:
        now = datetime.now().astimezone()
        timestamp = _parse_time(snapshot.get("update_time")) or now
        last_price = _decimal_optional(snapshot.get("last_price") or snapshot.get("cur_price"))
        previous_close = _decimal_optional(snapshot.get("prev_close_price"))
        open_price = _decimal_optional(snapshot.get("open_price"))
        cumulative_volume = _decimal_optional(snapshot.get("volume")) or Decimal("0")
        cumulative_turnover = _decimal_optional(snapshot.get("turnover")) or Decimal("0")
        prev_volume, prev_turnover = self._last_cumulative.get(symbol, (cumulative_volume, cumulative_turnover))
        volume_delta = max(cumulative_volume - prev_volume, Decimal("0"))
        turnover_delta = max(cumulative_turnover - prev_turnover, Decimal("0"))
        self._last_cumulative[symbol] = (cumulative_volume, cumulative_turnover)

        best_bid, bid_size, best_ask, ask_size = _book_summary(book)
        if best_bid is None:
            best_bid = _decimal_optional(snapshot.get("bid_price"))
            bid_size = _decimal_optional(snapshot.get("bid_vol")) or Decimal("0")
        if best_ask is None:
            best_ask = _decimal_optional(snapshot.get("ask_price"))
            ask_size = _decimal_optional(snapshot.get("ask_vol")) or Decimal("0")

        self._history[symbol].append(_RollingSample(timestamp, last_price, volume_delta, turnover_delta))
        history = list(self._history[symbol])
        prices = [sample.last_price for sample in history if sample.last_price is not None]
        rolling_inputs = [
            (sample.last_price, sample.volume_delta if sample.volume_delta > 0 else Decimal("1"))
            for sample in history
            if sample.last_price is not None
        ]
        age_now = now if timestamp.tzinfo is not None else now.replace(tzinfo=None)
        stale = (age_now - timestamp).total_seconds() > float(self.stale_after_seconds)
        return MarketState(
            symbol=symbol,
            timestamp=timestamp,
            interval_seconds=1,
            last_price=last_price,
            best_bid=best_bid,
            bid_size=bid_size,
            best_ask=best_ask,
            ask_size=ask_size,
            spread_bps=_spread_bps(best_bid, best_ask, last_price),
            orderbook_imbalance=_imbalance(bid_size, ask_size),
            opening_vwap=_safe_div(cumulative_turnover, cumulative_volume),
            rolling_vwap=_vwap(rolling_inputs),
            realized_vol=_realized_vol(prices),
            rolling_high=max(prices) if prices else None,
            rolling_low=min(prices) if prices else None,
            cumulative_volume=cumulative_volume,
            cumulative_turnover=cumulative_turnover,
            volume_delta=volume_delta,
            turnover_delta=turnover_delta,
            tick_count=len(prices),
            source="opend_live",
            previous_close=previous_close,
            open_price=open_price,
            market_state=str(snapshot.get("sec_status") or "") or None,
            stale=stale,
            orderbook_limited=best_bid is None or best_ask is None,
        )


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        return [dict(item) for item in to_dict("records")]
    return []


def _book_summary(book: dict[str, Any]) -> tuple[Decimal | None, Decimal, Decimal | None, Decimal]:
    bid_rows = book.get("Bid") or book.get("bid") or []
    ask_rows = book.get("Ask") or book.get("ask") or []
    return (
        _row_decimal(bid_rows, 0, "price"),
        _sum_qty(bid_rows),
        _row_decimal(ask_rows, 0, "price"),
        _sum_qty(ask_rows),
    )


def _row_decimal(rows: list[Any], index: int, key: str) -> Decimal | None:
    if not rows:
        return None
    row = rows[0]
    value = row.get(key) if isinstance(row, dict) else (row[index] if isinstance(row, (list, tuple)) and len(row) > index else None)
    return _decimal_optional(value)


def _sum_qty(rows: list[Any]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        if isinstance(row, dict):
            value = row.get("volume") or row.get("qty")
        else:
            value = row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else 0
        total += _decimal_optional(value) or Decimal("0")
    return total


def _parse_time(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo else value
    text = str(value).replace("/", "-")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _decimal_optional(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _vwap(inputs: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    total_volume = sum((qty for _, qty in inputs), Decimal("0"))
    if total_volume <= 0:
        return None
    return sum((price * qty for price, qty in inputs), Decimal("0")) / total_volume


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
