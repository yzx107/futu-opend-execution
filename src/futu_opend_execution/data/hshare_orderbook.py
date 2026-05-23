"""Probe and rebuild Hshare order-book semantics from order/trade rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ActiveOrder:
    side: str
    price: Decimal
    volume: int


@dataclass(slots=True)
class BookProbeMetrics:
    side_bit: int
    orders_total: int = 0
    add_count: int = 0
    modify_count: int = 0
    delete_count: int = 0
    modify_with_active_order: int = 0
    delete_with_active_order: int = 0
    volume_pre_checks: int = 0
    volume_pre_matches: int = 0
    level_checks: int = 0
    level_matches: int = 0
    book_observations: int = 0
    crossed_book_observations: int = 0
    trades_total: int = 0
    trades_with_book: int = 0
    trades_price_inside_book: int = 0
    bid_orderid_checks: int = 0
    bid_orderid_active: int = 0
    bid_orderid_side_matches: int = 0
    ask_orderid_checks: int = 0
    ask_orderid_active: int = 0
    ask_orderid_side_matches: int = 0
    final_active_orders: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "side_bit": self.side_bit,
            "orders_total": self.orders_total,
            "add_count": self.add_count,
            "modify_count": self.modify_count,
            "delete_count": self.delete_count,
            "modify_active_rate": _rate(self.modify_with_active_order, self.modify_count),
            "delete_active_rate": _rate(self.delete_with_active_order, self.delete_count),
            "volume_pre_match_rate": _rate(self.volume_pre_matches, self.volume_pre_checks),
            "level_match_rate": _rate(self.level_matches, self.level_checks),
            "crossed_book_rate": _rate(self.crossed_book_observations, self.book_observations),
            "trade_price_inside_book_rate": _rate(self.trades_price_inside_book, self.trades_with_book),
            "bid_orderid_active_rate": _rate(self.bid_orderid_active, self.bid_orderid_checks),
            "bid_orderid_side_match_rate": _rate(self.bid_orderid_side_matches, self.bid_orderid_checks),
            "ask_orderid_active_rate": _rate(self.ask_orderid_active, self.ask_orderid_checks),
            "ask_orderid_side_match_rate": _rate(self.ask_orderid_side_matches, self.ask_orderid_checks),
            "raw_counts": {
                "modify_with_active_order": self.modify_with_active_order,
                "delete_with_active_order": self.delete_with_active_order,
                "volume_pre_checks": self.volume_pre_checks,
                "volume_pre_matches": self.volume_pre_matches,
                "level_checks": self.level_checks,
                "level_matches": self.level_matches,
                "book_observations": self.book_observations,
                "crossed_book_observations": self.crossed_book_observations,
                "trades_total": self.trades_total,
                "trades_with_book": self.trades_with_book,
                "trades_price_inside_book": self.trades_price_inside_book,
                "bid_orderid_checks": self.bid_orderid_checks,
                "bid_orderid_active": self.bid_orderid_active,
                "bid_orderid_side_matches": self.bid_orderid_side_matches,
                "ask_orderid_checks": self.ask_orderid_checks,
                "ask_orderid_active": self.ask_orderid_active,
                "ask_orderid_side_matches": self.ask_orderid_side_matches,
                "final_active_orders": self.final_active_orders,
            },
        }


class HshareOrderBookProbe:
    def __init__(self, *, side_bit: int) -> None:
        if side_bit not in {0, 1}:
            raise ValueError("side_bit must be 0 or 1")
        self.side_bit = side_bit
        self.metrics = BookProbeMetrics(side_bit=side_bit)
        self.active_orders: dict[int, ActiveOrder] = {}
        self.book: dict[str, dict[Decimal, int]] = {"BID": {}, "ASK": {}}

    def apply_order(self, row: dict[str, Any]) -> None:
        order_type = _int(row.get("OrderType"))
        order_id = _int(row.get("OrderId"))
        if order_type is None or order_id is None:
            return
        self.metrics.orders_total += 1

        if order_type == 1:
            self.metrics.add_count += 1
            self._upsert_order(order_id, row)
        elif order_type == 2:
            self.metrics.modify_count += 1
            self._modify_order(order_id, row)
        elif order_type == 3:
            self.metrics.delete_count += 1
            self._delete_order(order_id)
        self._record_book_quality()

    def apply_trade_probe(self, row: dict[str, Any]) -> None:
        self.metrics.trades_total += 1
        trade_price = _decimal(row.get("Price"))
        best_bid, best_ask = self.best_bid_ask()
        if trade_price is not None and best_bid is not None and best_ask is not None and best_bid <= best_ask:
            self.metrics.trades_with_book += 1
            if best_bid <= trade_price <= best_ask:
                self.metrics.trades_price_inside_book += 1

        bid_id = _int(row.get("BidOrderID"))
        ask_id = _int(row.get("AskOrderID"))
        if bid_id and bid_id > 0:
            self.metrics.bid_orderid_checks += 1
            order = self.active_orders.get(bid_id)
            if order is not None:
                self.metrics.bid_orderid_active += 1
                if order.side == "BID":
                    self.metrics.bid_orderid_side_matches += 1
        if ask_id and ask_id > 0:
            self.metrics.ask_orderid_checks += 1
            order = self.active_orders.get(ask_id)
            if order is not None:
                self.metrics.ask_orderid_active += 1
                if order.side == "ASK":
                    self.metrics.ask_orderid_side_matches += 1

    def finish(self) -> BookProbeMetrics:
        self.metrics.final_active_orders = len(self.active_orders)
        return self.metrics

    def best_bid_ask(self) -> tuple[Decimal | None, Decimal | None]:
        bids = [price for price, qty in self.book["BID"].items() if qty > 0]
        asks = [price for price, qty in self.book["ASK"].items() if qty > 0]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def _upsert_order(self, order_id: int, row: dict[str, Any]) -> None:
        side = ext_side(row.get("Ext"), side_bit=self.side_bit)
        price = _decimal(row.get("Price"))
        volume = _int(row.get("Volume"))
        if side is None or price is None or volume is None or volume <= 0:
            return
        old = self.active_orders.get(order_id)
        if old is not None:
            self._remove_from_book(old)
        order = ActiveOrder(side=side, price=price, volume=volume)
        self.active_orders[order_id] = order
        self._add_to_book(order)
        self._check_level(row, order)

    def _modify_order(self, order_id: int, row: dict[str, Any]) -> None:
        old = self.active_orders.get(order_id)
        if old is not None:
            self.metrics.modify_with_active_order += 1
            volume_pre = _int(row.get("VolumePre"))
            if volume_pre is not None and volume_pre > 0:
                self.metrics.volume_pre_checks += 1
                if volume_pre == old.volume:
                    self.metrics.volume_pre_matches += 1
            self._remove_from_book(old)
        self._upsert_order(order_id, row)

    def _delete_order(self, order_id: int) -> None:
        old = self.active_orders.pop(order_id, None)
        if old is None:
            return
        self.metrics.delete_with_active_order += 1
        self._remove_from_book(old)

    def _add_to_book(self, order: ActiveOrder) -> None:
        self.book[order.side][order.price] = self.book[order.side].get(order.price, 0) + order.volume

    def _remove_from_book(self, order: ActiveOrder) -> None:
        next_qty = self.book[order.side].get(order.price, 0) - order.volume
        if next_qty > 0:
            self.book[order.side][order.price] = next_qty
        else:
            self.book[order.side].pop(order.price, None)

    def _check_level(self, row: dict[str, Any], order: ActiveOrder) -> None:
        level = _int(row.get("Level"))
        if level is None or level < 0:
            return
        self.metrics.level_checks += 1
        prices = sorted(self.book[order.side], reverse=order.side == "BID")
        try:
            computed = prices.index(order.price)
        except ValueError:
            return
        if computed == level:
            self.metrics.level_matches += 1

    def _record_book_quality(self) -> None:
        best_bid, best_ask = self.best_bid_ask()
        if best_bid is None or best_ask is None:
            return
        self.metrics.book_observations += 1
        if best_bid > best_ask:
            self.metrics.crossed_book_observations += 1


def probe_orderbook_semantics(
    *,
    order_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    events = [("order", row) for row in order_rows] + [("trade", row) for row in trade_rows]
    events.sort(key=lambda item: (_sort_time(item[1]), _int(item[1].get("SeqNum")) or _int(item[1].get("TickID")) or 0, 0 if item[0] == "order" else 1))
    results = []
    for side_bit in (0, 1):
        probe = HshareOrderBookProbe(side_bit=side_bit)
        for kind, row in events:
            if kind == "order":
                probe.apply_order(row)
            else:
                probe.apply_trade_probe(row)
        results.append(probe.finish().to_jsonable())
    recommended = min(
        results,
        key=lambda item: (
            item["crossed_book_rate"] if item["crossed_book_rate"] is not None else 1,
            -(item["bid_orderid_side_match_rate"] or 0),
            -(item["ask_orderid_side_match_rate"] or 0),
            -(item["level_match_rate"] or 0),
        ),
    )
    return {
        "candidate_results": results,
        "recommended_side_bit": recommended["side_bit"],
        "recommendation_basis": "min crossed_book_rate, then max order-id side match, then max level match",
        "notes": [
            "Ext[0] is vendor-described order side; Ext[1] is vendor-described level side.",
            "Use this as research evidence only until linkage and book sanity rates are acceptable.",
        ],
    }


def ext_side(ext: Any, *, side_bit: int) -> str | None:
    text = str(ext or "").strip()
    if len(text) <= side_bit:
        return None
    if text[side_bit] == "0":
        return "BID"
    if text[side_bit] == "1":
        return "ASK"
    return None


def symbol_code(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        normalized = normalized.split(".", 1)[1]
    return normalized.zfill(5)


def default_probe_paths(data_root: Path | str, *, date: str) -> tuple[Path, Path]:
    root = Path(data_root)
    return (
        root / "orders" / f"date={date}" / f"{date.replace('-', '')}_orders.parquet",
        root / "trades" / f"date={date}" / f"{date.replace('-', '')}_trades.parquet",
    )


def _sort_time(row: dict[str, Any]) -> str:
    value = row.get("SendTime") or row.get("Time") or ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _int(value: Any) -> int | None:
    if _is_missing(value):
        return None
    return int(float(value))


def _decimal(value: Any) -> Decimal | None:
    if _is_missing(value):
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    try:
        return bool(math.isnan(value))
    except TypeError:
        return False
