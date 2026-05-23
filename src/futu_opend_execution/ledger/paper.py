"""Append-only paper ledger for cost reducer round trips."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class _OpenSell:
    timestamp: str
    symbol: str
    quantity: int
    price: Decimal
    event_id: str | None = None


class PaperLedger:
    def __init__(self, path: Path | str, *, roundtrip_cost_bps: Decimal | str | int | float = Decimal("35")) -> None:
        self.path = Path(path)
        self.roundtrip_cost_bps = _decimal(roundtrip_cost_bps)
        self._seen_event_ids, self._open_sells = self._load_state()

    def record_trade(
        self,
        *,
        symbol: str,
        action: str,
        quantity: int,
        price: Decimal | str | int | float,
        timestamp: str | None = None,
        reason: str = "",
        event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if event_id and event_id in self._seen_event_ids:
            return None
        price_decimal = _decimal(price)
        if quantity <= 0 or price_decimal <= 0:
            return None
        ts = timestamp or datetime.now().astimezone().isoformat(timespec="seconds")
        normalized_action = action.upper()
        if normalized_action == "SELL_TRADING":
            row = self._record_sell(symbol, quantity, price_decimal, ts, reason, event_id)
        elif normalized_action == "REBUY_TRADING":
            row = self._record_rebuy(symbol, quantity, price_decimal, ts, reason, event_id)
        else:
            return None
        if event_id:
            self._seen_event_ids.add(event_id)
        return row

    def record_event(self, event: Any, *, timestamp: str | None = None, event_id: str | None = None) -> dict[str, Any] | None:
        limit_price = getattr(event, "limit_price", None)
        if limit_price is None:
            return None
        action = getattr(getattr(event, "action", None), "value", getattr(event, "action", ""))
        return self.record_trade(
            symbol=str(getattr(event, "symbol", "")),
            action=str(action),
            quantity=int(getattr(event, "quantity", 0)),
            price=limit_price,
            timestamp=timestamp,
            reason=str(getattr(event, "reason", "")),
            event_id=event_id,
        )

    @property
    def open_quantity(self) -> int:
        return sum(item.quantity for item in self._open_sells)

    def _record_sell(
        self,
        symbol: str,
        quantity: int,
        price: Decimal,
        timestamp: str,
        reason: str,
        event_id: str | None,
    ) -> dict[str, Any]:
        row = {
            "timestamp": timestamp,
            "event_id": event_id,
            "symbol": _normalize_symbol(symbol),
            "entry_type": "SELL_OPEN",
            "action": "SELL_TRADING",
            "quantity": quantity,
            "price": _str_decimal(price),
            "gross_pnl": "0",
            "estimated_cost": "0",
            "net_pnl": "0",
            "open_quantity_after": self.open_quantity + quantity,
            "reason": reason,
        }
        self._open_sells.append(_OpenSell(timestamp, row["symbol"], quantity, price, event_id))
        self._append(row)
        return row

    def _record_rebuy(
        self,
        symbol: str,
        quantity: int,
        price: Decimal,
        timestamp: str,
        reason: str,
        event_id: str | None,
    ) -> dict[str, Any] | None:
        symbol = _normalize_symbol(symbol)
        index = next((i for i, item in enumerate(self._open_sells) if item.symbol == symbol), None)
        if index is None:
            return None
        sell = self._open_sells[index]
        matched_qty = min(quantity, sell.quantity)
        gross_pnl = (sell.price - price) * Decimal(matched_qty)
        estimated_cost = (sell.price + price) * Decimal(matched_qty) * self.roundtrip_cost_bps / Decimal("10000")
        net_pnl = gross_pnl - estimated_cost
        sell.quantity -= matched_qty
        if sell.quantity <= 0:
            self._open_sells.pop(index)
        row = {
            "timestamp": timestamp,
            "event_id": event_id,
            "symbol": symbol,
            "entry_type": "ROUND_TRIP_CLOSE",
            "action": "REBUY_TRADING",
            "quantity": matched_qty,
            "price": _str_decimal(price),
            "sell_price": _str_decimal(sell.price),
            "gross_pnl": _str_decimal(gross_pnl),
            "estimated_cost": _str_decimal(estimated_cost),
            "net_pnl": _str_decimal(net_pnl),
            "open_quantity_after": self.open_quantity,
            "reason": reason,
        }
        self._append(row)
        return row

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load_state(self) -> tuple[set[str], list[_OpenSell]]:
        seen: set[str] = set()
        open_sells: list[_OpenSell] = []
        for row in _read_rows(self.path):
            event_id = row.get("event_id")
            if event_id:
                seen.add(str(event_id))
            if row.get("entry_type") == "SELL_OPEN":
                open_sells.append(_OpenSell(str(row["timestamp"]), str(row["symbol"]), int(row["quantity"]), _decimal(row["price"]), event_id))
            elif row.get("entry_type") == "ROUND_TRIP_CLOSE":
                remaining = int(row.get("quantity", 0) or 0)
                symbol = str(row.get("symbol", ""))
                while remaining > 0:
                    index = next((i for i, item in enumerate(open_sells) if item.symbol == symbol), None)
                    if index is None:
                        break
                    consume = min(remaining, open_sells[index].quantity)
                    open_sells[index].quantity -= consume
                    remaining -= consume
                    if open_sells[index].quantity <= 0:
                        open_sells.pop(index)
        return seen, open_sells


def summarize_paper_ledger(path: Path | str) -> dict[str, Any]:
    rows = _read_rows(path)
    closes = [row for row in rows if row.get("entry_type") == "ROUND_TRIP_CLOSE"]
    return {
        "sell_count": sum(1 for row in rows if row.get("entry_type") == "SELL_OPEN"),
        "rebuy_count": len(closes),
        "round_trips": len(closes),
        "net_pnl": _str_decimal(sum((_decimal(row.get("net_pnl")) for row in closes), Decimal("0"))),
        "open_quantity": int(rows[-1].get("open_quantity_after", 0)) if rows else 0,
    }


def _read_rows(path: Path | str) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        normalized = f"HK.{normalized}"
    return normalized


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
