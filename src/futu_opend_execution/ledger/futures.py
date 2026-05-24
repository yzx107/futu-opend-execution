"""Append-only paper ledger for futures open/close accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution._compat import StrEnum
from futu_opend_execution.contracts import ContractSpec, ContractSpecError


class FuturesAction(StrEnum):
    BUY_OPEN = "BUY_OPEN"
    SELL_OPEN = "SELL_OPEN"
    SELL_CLOSE = "SELL_CLOSE"
    BUY_CLOSE = "BUY_CLOSE"


class FuturesDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class FuturesLedgerError(ValueError):
    """Raised when a futures paper fill cannot be applied safely."""


@dataclass(slots=True)
class _OpenLot:
    timestamp: str
    symbol: str
    direction: FuturesDirection
    quantity: int
    price: Decimal
    event_id: str | None = None


class FuturesPaperLedger:
    def __init__(self, path: Path | str, *, contracts: dict[str, ContractSpec]) -> None:
        self.path = Path(path)
        self.contracts = {symbol.upper(): spec for symbol, spec in contracts.items()}
        self._seen_event_ids, self._open_lots = self._load_state()

    def record_fill(
        self,
        *,
        symbol: str,
        action: str | FuturesAction,
        quantity: int,
        price: Decimal | str | int | float,
        timestamp: str | None = None,
        event_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any] | None:
        normalized_symbol = symbol.strip().upper()
        if event_id and event_id in self._seen_event_ids:
            return None
        spec = self._spec(normalized_symbol)
        normalized_action = _action(action)
        spec.validate_quantity(quantity)
        price_decimal = spec.validate_price(price)
        ts = timestamp or datetime.now().astimezone().isoformat(timespec="seconds")
        if normalized_action in {FuturesAction.BUY_OPEN, FuturesAction.SELL_OPEN}:
            row = self._record_open(spec, normalized_action, quantity, price_decimal, ts, event_id, reason)
        else:
            row = self._record_close(spec, normalized_action, quantity, price_decimal, ts, event_id, reason)
        if event_id:
            self._seen_event_ids.add(event_id)
        return row

    @property
    def open_positions(self) -> dict[str, dict[str, int]]:
        positions: dict[str, dict[str, int]] = {}
        for lot in self._open_lots:
            side = positions.setdefault(lot.symbol, {"LONG": 0, "SHORT": 0})
            side[lot.direction.value] += lot.quantity
        return positions

    def summary(self, *, mark_prices: dict[str, Decimal | str | int | float] | None = None) -> dict[str, Any]:
        return summarize_futures_paper_ledger(self.path, contracts=self.contracts, mark_prices=mark_prices)

    def _record_open(
        self,
        spec: ContractSpec,
        action: FuturesAction,
        quantity: int,
        price: Decimal,
        timestamp: str,
        event_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        direction = FuturesDirection.LONG if action is FuturesAction.BUY_OPEN else FuturesDirection.SHORT
        commission = spec.commission_per_contract * Decimal(quantity)
        row = {
            "event": "futures_paper_fill",
            "timestamp": timestamp,
            "event_id": event_id,
            "symbol": spec.symbol,
            "action": action.value,
            "direction": direction.value,
            "entry_type": "OPEN",
            "quantity": quantity,
            "price": _str_decimal(price),
            "multiplier": _str_decimal(spec.contract_multiplier),
            "notional": _str_decimal(spec.notional(price=price, quantity=quantity)),
            "initial_margin": _str_decimal(spec.initial_margin(price=price, quantity=quantity)),
            "commission": _str_decimal(commission),
            "gross_pnl": "0",
            "net_pnl": _str_decimal(-commission),
            "open_positions_after": self._positions_after(spec.symbol, direction, quantity),
            "reason": reason,
        }
        self._open_lots.append(_OpenLot(timestamp, spec.symbol, direction, quantity, price, event_id))
        self._append(row)
        return row

    def _record_close(
        self,
        spec: ContractSpec,
        action: FuturesAction,
        quantity: int,
        price: Decimal,
        timestamp: str,
        event_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        direction = FuturesDirection.LONG if action is FuturesAction.SELL_CLOSE else FuturesDirection.SHORT
        available = sum(lot.quantity for lot in self._open_lots if lot.symbol == spec.symbol and lot.direction is direction)
        if quantity > available:
            raise FuturesLedgerError("close quantity exceeds open position")
        remaining = quantity
        gross_pnl = Decimal("0")
        matched: list[dict[str, Any]] = []
        while remaining > 0:
            index = next((i for i, lot in enumerate(self._open_lots) if lot.symbol == spec.symbol and lot.direction is direction), None)
            if index is None:  # pragma: no cover - guarded by available check
                break
            lot = self._open_lots[index]
            consume = min(remaining, lot.quantity)
            gross_pnl += _lot_pnl(direction=direction, open_price=lot.price, close_price=price, quantity=consume, multiplier=spec.contract_multiplier)
            matched.append(
                {
                    "timestamp": lot.timestamp,
                    "direction": lot.direction.value,
                    "quantity": consume,
                    "price": _str_decimal(lot.price),
                }
            )
            lot.quantity -= consume
            remaining -= consume
            if lot.quantity <= 0:
                self._open_lots.pop(index)
        commission = spec.commission_per_contract * Decimal(quantity)
        net_pnl = gross_pnl - commission
        row = {
            "event": "futures_paper_fill",
            "timestamp": timestamp,
            "event_id": event_id,
            "symbol": spec.symbol,
            "action": action.value,
            "direction": direction.value,
            "entry_type": "CLOSE",
            "quantity": quantity,
            "price": _str_decimal(price),
            "multiplier": _str_decimal(spec.contract_multiplier),
            "matched_lots": matched,
            "notional": _str_decimal(spec.notional(price=price, quantity=quantity)),
            "initial_margin": "0",
            "commission": _str_decimal(commission),
            "gross_pnl": _str_decimal(gross_pnl),
            "net_pnl": _str_decimal(net_pnl),
            "open_positions_after": self.open_positions,
            "reason": reason,
        }
        self._append(row)
        return row

    def _positions_after(self, symbol: str, direction: FuturesDirection, quantity_delta: int) -> dict[str, dict[str, int]]:
        positions = self.open_positions
        side = positions.setdefault(symbol, {"LONG": 0, "SHORT": 0})
        side[direction.value] += quantity_delta
        return positions

    def _spec(self, symbol: str) -> ContractSpec:
        try:
            return self.contracts[symbol]
        except KeyError as exc:
            raise FuturesLedgerError(f"unknown futures contract: {symbol}") from exc

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load_state(self) -> tuple[set[str], list[_OpenLot]]:
        seen: set[str] = set()
        lots: list[_OpenLot] = []
        for row in _read_rows(self.path):
            event_id = row.get("event_id")
            if event_id:
                seen.add(str(event_id))
            if row.get("entry_type") == "OPEN":
                lots.append(
                    _OpenLot(
                        timestamp=str(row["timestamp"]),
                        symbol=str(row["symbol"]),
                        direction=FuturesDirection(str(row["direction"])),
                        quantity=int(row["quantity"]),
                        price=_decimal(row["price"]),
                        event_id=str(event_id) if event_id else None,
                    )
                )
            elif row.get("entry_type") == "CLOSE":
                _consume_lots(lots, symbol=str(row["symbol"]), direction=FuturesDirection(str(row["direction"])), quantity=int(row["quantity"]))
        return seen, lots


def summarize_futures_paper_ledger(
    path: Path | str,
    *,
    contracts: dict[str, ContractSpec],
    mark_prices: dict[str, Decimal | str | int | float] | None = None,
) -> dict[str, Any]:
    rows = _read_rows(path)
    lots: list[_OpenLot] = []
    realized_gross = Decimal("0")
    net_pnl = Decimal("0")
    commission = Decimal("0")
    for row in rows:
        commission += _decimal(row.get("commission"))
        net_pnl += _decimal(row.get("net_pnl"))
        if row.get("entry_type") == "OPEN":
            lots.append(
                _OpenLot(
                    timestamp=str(row["timestamp"]),
                    symbol=str(row["symbol"]),
                    direction=FuturesDirection(str(row["direction"])),
                    quantity=int(row["quantity"]),
                    price=_decimal(row["price"]),
                )
            )
        elif row.get("entry_type") == "CLOSE":
            realized_gross += _decimal(row.get("gross_pnl"))
            _consume_lots(lots, symbol=str(row["symbol"]), direction=FuturesDirection(str(row["direction"])), quantity=int(row["quantity"]))
    marks = {key.upper(): _decimal(value) for key, value in (mark_prices or {}).items()}
    unrealized_gross = Decimal("0")
    margin_used = Decimal("0")
    open_positions: dict[str, dict[str, int]] = {}
    for lot in lots:
        spec = contracts[lot.symbol]
        mark = marks.get(lot.symbol, lot.price)
        unrealized_gross += _lot_pnl(direction=lot.direction, open_price=lot.price, close_price=mark, quantity=lot.quantity, multiplier=spec.contract_multiplier)
        margin_used += spec.initial_margin(price=mark, quantity=lot.quantity)
        side = open_positions.setdefault(lot.symbol, {"LONG": 0, "SHORT": 0})
        side[lot.direction.value] += lot.quantity
    return {
        "event": "futures_paper_summary",
        "fill_count": len(rows),
        "open_count": sum(1 for row in rows if row.get("entry_type") == "OPEN"),
        "close_count": sum(1 for row in rows if row.get("entry_type") == "CLOSE"),
        "realized_gross_pnl": _str_decimal(realized_gross),
        "total_commission": _str_decimal(commission),
        "realized_net_pnl": _str_decimal(net_pnl),
        "unrealized_gross_pnl": _str_decimal(unrealized_gross),
        "margin_used": _str_decimal(margin_used),
        "open_positions": open_positions,
    }


def _consume_lots(lots: list[_OpenLot], *, symbol: str, direction: FuturesDirection, quantity: int) -> None:
    remaining = quantity
    while remaining > 0:
        index = next((i for i, lot in enumerate(lots) if lot.symbol == symbol and lot.direction is direction), None)
        if index is None:
            break
        consume = min(remaining, lots[index].quantity)
        lots[index].quantity -= consume
        remaining -= consume
        if lots[index].quantity <= 0:
            lots.pop(index)


def _lot_pnl(*, direction: FuturesDirection, open_price: Decimal, close_price: Decimal, quantity: int, multiplier: Decimal) -> Decimal:
    if direction is FuturesDirection.LONG:
        return (close_price - open_price) * Decimal(quantity) * multiplier
    return (open_price - close_price) * Decimal(quantity) * multiplier


def _read_rows(path: Path | str) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def _action(value: str | FuturesAction) -> FuturesAction:
    if isinstance(value, FuturesAction):
        return value
    try:
        return FuturesAction(str(value).strip().upper())
    except ValueError as exc:
        raise FuturesLedgerError(f"unsupported futures action: {value}") from exc


def _decimal(value: Any) -> Decimal:
    if value in {None, ""}:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
