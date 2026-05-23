"""User-owned watchlist configuration for dry-run trading-agent loops."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class WatchlistConfigError(ValueError):
    """Raised when a watchlist config is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class BlackSwanThresholds:
    intraday_drop_bps: Decimal
    gap_down_bps: Decimal
    spread_bps: Decimal
    stale_seconds: Decimal
    min_bid_size_lots: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlackSwanThresholds":
        _require_keys(
            data,
            {
                "intraday_drop_bps",
                "gap_down_bps",
                "spread_bps",
                "stale_seconds",
                "min_bid_size_lots",
            },
            "black_swan_thresholds",
        )
        item = cls(
            intraday_drop_bps=_decimal(data["intraday_drop_bps"], "black_swan_thresholds.intraday_drop_bps"),
            gap_down_bps=_decimal(data["gap_down_bps"], "black_swan_thresholds.gap_down_bps"),
            spread_bps=_decimal(data["spread_bps"], "black_swan_thresholds.spread_bps"),
            stale_seconds=_decimal(data["stale_seconds"], "black_swan_thresholds.stale_seconds"),
            min_bid_size_lots=_int(data["min_bid_size_lots"], "black_swan_thresholds.min_bid_size_lots"),
        )
        for name in ("intraday_drop_bps", "gap_down_bps", "spread_bps", "stale_seconds"):
            if getattr(item, name) < 0:
                raise WatchlistConfigError(f"black_swan_thresholds.{name} must be non-negative")
        if item.min_bid_size_lots < 0:
            raise WatchlistConfigError("black_swan_thresholds.min_bid_size_lots must be non-negative")
        return item

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "intraday_drop_bps": _str_decimal(self.intraday_drop_bps),
            "gap_down_bps": _str_decimal(self.gap_down_bps),
            "spread_bps": _str_decimal(self.spread_bps),
            "stale_seconds": _str_decimal(self.stale_seconds),
            "min_bid_size_lots": self.min_bid_size_lots,
        }


@dataclass(frozen=True, slots=True)
class CostReducerSymbolRules:
    overextension_vol_multiple: Decimal
    high_pullback_vol_multiple: Decimal
    rebuy_anchor_vol_band: Decimal
    max_spread_bps: Decimal
    estimated_roundtrip_cost_bps: Decimal
    safety_buffer_bps: Decimal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CostReducerSymbolRules":
        _require_keys(
            data,
            {
                "overextension_vol_multiple",
                "high_pullback_vol_multiple",
                "rebuy_anchor_vol_band",
                "max_spread_bps",
                "estimated_roundtrip_cost_bps",
                "safety_buffer_bps",
            },
            "cost_reducer_rules",
        )
        item = cls(
            overextension_vol_multiple=_decimal(data["overextension_vol_multiple"], "cost_reducer_rules.overextension_vol_multiple"),
            high_pullback_vol_multiple=_decimal(data["high_pullback_vol_multiple"], "cost_reducer_rules.high_pullback_vol_multiple"),
            rebuy_anchor_vol_band=_decimal(data["rebuy_anchor_vol_band"], "cost_reducer_rules.rebuy_anchor_vol_band"),
            max_spread_bps=_decimal(data["max_spread_bps"], "cost_reducer_rules.max_spread_bps"),
            estimated_roundtrip_cost_bps=_decimal(data["estimated_roundtrip_cost_bps"], "cost_reducer_rules.estimated_roundtrip_cost_bps"),
            safety_buffer_bps=_decimal(data["safety_buffer_bps"], "cost_reducer_rules.safety_buffer_bps"),
        )
        for name in (
            "overextension_vol_multiple",
            "high_pullback_vol_multiple",
            "rebuy_anchor_vol_band",
            "max_spread_bps",
            "estimated_roundtrip_cost_bps",
            "safety_buffer_bps",
        ):
            if getattr(item, name) < 0:
                raise WatchlistConfigError(f"cost_reducer_rules.{name} must be non-negative")
        return item

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "overextension_vol_multiple": _str_decimal(self.overextension_vol_multiple),
            "high_pullback_vol_multiple": _str_decimal(self.high_pullback_vol_multiple),
            "rebuy_anchor_vol_band": _str_decimal(self.rebuy_anchor_vol_band),
            "max_spread_bps": _str_decimal(self.max_spread_bps),
            "estimated_roundtrip_cost_bps": _str_decimal(self.estimated_roundtrip_cost_bps),
            "safety_buffer_bps": _str_decimal(self.safety_buffer_bps),
        }


@dataclass(frozen=True, slots=True)
class WatchSymbolConfig:
    symbol: str
    enabled: bool
    lot_size: int
    current_qty: int
    cost_price: Decimal
    core_qty_target: int
    trading_qty_target: int
    max_sell_qty_per_order: int
    max_rebuy_qty_per_order: int
    max_sell_total_position_ratio: Decimal
    max_round_trips: int
    black_swan_thresholds: BlackSwanThresholds
    cost_reducer_rules: CostReducerSymbolRules
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchSymbolConfig":
        _require_keys(
            data,
            {
                "symbol",
                "enabled",
                "lot_size",
                "current_qty",
                "cost_price",
                "core_qty_target",
                "trading_qty_target",
                "max_sell_qty_per_order",
                "max_rebuy_qty_per_order",
                "max_sell_total_position_ratio",
                "max_round_trips",
                "black_swan_thresholds",
                "cost_reducer_rules",
            },
            "symbol config",
        )
        item = cls(
            symbol=normalize_hk_symbol(str(data["symbol"])),
            enabled=bool(data["enabled"]),
            lot_size=_int(data["lot_size"], "lot_size"),
            current_qty=_int(data["current_qty"], "current_qty"),
            cost_price=_decimal(data["cost_price"], "cost_price"),
            core_qty_target=_int(data["core_qty_target"], "core_qty_target"),
            trading_qty_target=_int(data["trading_qty_target"], "trading_qty_target"),
            max_sell_qty_per_order=_int(data["max_sell_qty_per_order"], "max_sell_qty_per_order"),
            max_rebuy_qty_per_order=_int(data["max_rebuy_qty_per_order"], "max_rebuy_qty_per_order"),
            max_sell_total_position_ratio=_decimal(data["max_sell_total_position_ratio"], "max_sell_total_position_ratio"),
            max_round_trips=_int(data["max_round_trips"], "max_round_trips"),
            black_swan_thresholds=BlackSwanThresholds.from_dict(_dict(data["black_swan_thresholds"], "black_swan_thresholds")),
            cost_reducer_rules=CostReducerSymbolRules.from_dict(_dict(data["cost_reducer_rules"], "cost_reducer_rules")),
            notes=str(data.get("notes", "")),
        )
        item.validate()
        return item

    def validate(self) -> None:
        if self.lot_size <= 0:
            raise WatchlistConfigError(f"{self.symbol}: lot_size must be positive")
        if self.current_qty <= 0:
            raise WatchlistConfigError(f"{self.symbol}: current_qty must be positive")
        if self.current_qty % self.lot_size != 0:
            raise WatchlistConfigError(f"{self.symbol}: current_qty must be lot-aligned")
        if self.core_qty_target + self.trading_qty_target != self.current_qty:
            raise WatchlistConfigError(f"{self.symbol}: core_qty_target + trading_qty_target must equal current_qty")
        for name in ("core_qty_target", "trading_qty_target", "max_sell_qty_per_order", "max_rebuy_qty_per_order"):
            value = getattr(self, name)
            if value <= 0:
                raise WatchlistConfigError(f"{self.symbol}: {name} must be positive")
            if value % self.lot_size != 0:
                raise WatchlistConfigError(f"{self.symbol}: {name} must be lot-aligned")
        if self.core_qty_target < self.lot_size:
            raise WatchlistConfigError(f"{self.symbol}: core_qty_target must contain at least one lot")
        if self.trading_qty_target < self.lot_size:
            raise WatchlistConfigError(f"{self.symbol}: trading_qty_target must contain at least one lot")
        if self.cost_price <= 0:
            raise WatchlistConfigError(f"{self.symbol}: cost_price must be positive")
        if not (Decimal("0") <= self.max_sell_total_position_ratio <= Decimal("1")):
            raise WatchlistConfigError(f"{self.symbol}: max_sell_total_position_ratio must be between 0 and 1")
        if self.max_round_trips < 0:
            raise WatchlistConfigError(f"{self.symbol}: max_round_trips must be non-negative")

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "enabled": self.enabled,
            "lot_size": self.lot_size,
            "current_qty": self.current_qty,
            "cost_price": _str_decimal(self.cost_price),
            "core_qty_target": self.core_qty_target,
            "trading_qty_target": self.trading_qty_target,
            "max_sell_qty_per_order": self.max_sell_qty_per_order,
            "max_rebuy_qty_per_order": self.max_rebuy_qty_per_order,
            "max_sell_total_position_ratio": _str_decimal(self.max_sell_total_position_ratio),
            "max_round_trips": self.max_round_trips,
            "black_swan_thresholds": self.black_swan_thresholds.to_jsonable(),
            "cost_reducer_rules": self.cost_reducer_rules.to_jsonable(),
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class WatchlistConfig:
    symbols: tuple[WatchSymbolConfig, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchlistConfig":
        _require_keys(data, {"symbols"}, "watchlist")
        raw_symbols = data["symbols"]
        if not isinstance(raw_symbols, list) or not raw_symbols:
            raise WatchlistConfigError("watchlist.symbols must be a non-empty list")
        symbols = tuple(WatchSymbolConfig.from_dict(_dict(item, "symbols[]")) for item in raw_symbols)
        seen: set[str] = set()
        for item in symbols:
            if item.symbol in seen:
                raise WatchlistConfigError(f"{item.symbol}: duplicate symbol")
            seen.add(item.symbol)
        return cls(symbols=symbols)

    @property
    def enabled_symbols(self) -> tuple[WatchSymbolConfig, ...]:
        return tuple(item for item in self.symbols if item.enabled)

    def to_jsonable(self) -> dict[str, Any]:
        return {"symbols": [item.to_jsonable() for item in self.symbols]}


def load_watchlist_config(path: Path | str) -> WatchlistConfig:
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WatchlistConfigError(f"{target}: invalid JSON: {exc}") from exc
    return WatchlistConfig.from_dict(_dict(raw, "watchlist"))


def normalize_hk_symbol(symbol: str) -> str:
    text = symbol.strip().upper()
    match = re.fullmatch(r"(?:HK\.)?(\d{1,5})", text)
    if not match:
        raise WatchlistConfigError(f"symbol must be HK.XXXXX or 1-5 HK digits: {symbol!r}")
    return f"HK.{match.group(1).zfill(5)}"


def _require_keys(data: dict[str, Any], keys: set[str], context: str) -> None:
    missing = sorted(keys - set(data))
    if missing:
        raise WatchlistConfigError(f"{context} missing required field(s): {', '.join(missing)}")


def _dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchlistConfigError(f"{name} must be an object")
    return value


def _int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WatchlistConfigError(f"{name} must be an integer") from exc
    if str(value).strip() not in {str(result), f"{result}.0"} and not isinstance(value, int):
        raise WatchlistConfigError(f"{name} must be an integer")
    return result


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise WatchlistConfigError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise WatchlistConfigError(f"{name} must be finite")
    return result


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
