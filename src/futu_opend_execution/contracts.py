"""Tradable contract specifications.

The first non-stock asset class this repo needs is index futures.  Keep the
model deliberately small: one constant tick size, one multiplier, one margin
rate, and explicit caveats in config rather than inferred exchange behavior.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution._compat import StrEnum


class AssetClass(StrEnum):
    HK_EQUITY = "HK_EQUITY"
    INDEX_FUTURE = "INDEX_FUTURE"


class ContractSpecError(ValueError):
    """Raised when a contract spec or order input is invalid."""


@dataclass(frozen=True, slots=True)
class ContractSpec:
    symbol: str
    exchange: str
    asset_class: AssetClass | str
    contract_multiplier: Decimal | str | int | float
    tick_size: Decimal | str | int | float
    currency: str = "HKD"
    min_order_size: int = 1
    margin_rate: Decimal | str | int | float = Decimal("0")
    commission_per_contract: Decimal | str | int | float = Decimal("0")
    session_timezone: str = "Asia/Hong_Kong"
    trading_sessions: tuple[str, ...] = ()
    expiry_date: str | None = None
    rollover_group: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "exchange", self.exchange.strip().upper())
        object.__setattr__(self, "asset_class", _asset_class(self.asset_class))
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "contract_multiplier", _decimal(self.contract_multiplier))
        object.__setattr__(self, "tick_size", _decimal(self.tick_size))
        object.__setattr__(self, "margin_rate", _decimal(self.margin_rate))
        object.__setattr__(self, "commission_per_contract", _decimal(self.commission_per_contract))
        object.__setattr__(self, "trading_sessions", tuple(self.trading_sessions))
        if not self.symbol:
            raise ContractSpecError("symbol is required")
        if not self.exchange:
            raise ContractSpecError("exchange is required")
        if self.contract_multiplier <= 0:
            raise ContractSpecError("contract_multiplier must be positive")
        if self.tick_size <= 0:
            raise ContractSpecError("tick_size must be positive")
        if self.min_order_size <= 0:
            raise ContractSpecError("min_order_size must be positive")
        if self.margin_rate < 0:
            raise ContractSpecError("margin_rate must be non-negative")
        if self.commission_per_contract < 0:
            raise ContractSpecError("commission_per_contract must be non-negative")

    @property
    def tick_value(self) -> Decimal:
        return self.tick_size * self.contract_multiplier

    def validate_quantity(self, quantity: int) -> None:
        if quantity <= 0:
            raise ContractSpecError("quantity must be positive")
        if quantity % self.min_order_size != 0:
            raise ContractSpecError("quantity must align to min_order_size")

    def validate_price(self, price: Decimal | str | int | float) -> Decimal:
        value = _decimal(price)
        if value <= 0:
            raise ContractSpecError("price must be positive")
        if value % self.tick_size != 0:
            raise ContractSpecError("price must align to tick_size")
        return value

    def notional(self, *, price: Decimal | str | int | float, quantity: int) -> Decimal:
        self.validate_quantity(quantity)
        return self.validate_price(price) * self.contract_multiplier * Decimal(quantity)

    def initial_margin(self, *, price: Decimal | str | int | float, quantity: int) -> Decimal:
        return self.notional(price=price, quantity=quantity) * self.margin_rate

    def to_jsonable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_class"] = self.asset_class.value
        payload["contract_multiplier"] = _str_decimal(self.contract_multiplier)
        payload["tick_size"] = _str_decimal(self.tick_size)
        payload["tick_value"] = _str_decimal(self.tick_value)
        payload["margin_rate"] = _str_decimal(self.margin_rate)
        payload["commission_per_contract"] = _str_decimal(self.commission_per_contract)
        payload["trading_sessions"] = list(self.trading_sessions)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ContractSpec":
        return cls(
            symbol=str(payload["symbol"]),
            exchange=str(payload["exchange"]),
            asset_class=payload.get("asset_class", AssetClass.INDEX_FUTURE),
            contract_multiplier=payload["contract_multiplier"],
            tick_size=payload["tick_size"],
            currency=str(payload.get("currency", "HKD")),
            min_order_size=int(payload.get("min_order_size", 1)),
            margin_rate=payload.get("margin_rate", 0),
            commission_per_contract=payload.get("commission_per_contract", 0),
            session_timezone=str(payload.get("session_timezone", "Asia/Hong_Kong")),
            trading_sessions=tuple(payload.get("trading_sessions") or ()),
            expiry_date=payload.get("expiry_date"),
            rollover_group=payload.get("rollover_group"),
            notes=str(payload.get("notes", "")),
        )


def load_contract_specs(path: Path | str) -> dict[str, ContractSpec]:
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8"))
    rows = data.get("contracts", data if isinstance(data, list) else None)
    if not isinstance(rows, list):
        raise ContractSpecError("contract config must be a list or contain contracts")
    specs = [ContractSpec.from_dict(row) for row in rows]
    return {spec.symbol: spec for spec in specs}


def write_contract_specs(specs: dict[str, ContractSpec], path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [spec.to_jsonable() for spec in specs.values()]
    target.write_text(json.dumps({"contracts": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _asset_class(value: AssetClass | str) -> AssetClass:
    if isinstance(value, AssetClass):
        return value
    try:
        return AssetClass(str(value).strip().upper())
    except ValueError as exc:
        raise ContractSpecError(f"unsupported asset_class: {value}") from exc


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
