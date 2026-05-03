"""Runtime strategy configuration for grey-market execution workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution._compat import StrEnum


class ExecutionMode(StrEnum):
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    LIVE_DRY_RUN = "LIVE_DRY_RUN"
    LIVE_REAL_BUY_ONLY = "LIVE_REAL_BUY_ONLY"
    LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL = "LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL"
    LIVE_REAL_COST_REDUCER_AUTO = "LIVE_REAL_COST_REDUCER_AUTO"


@dataclass(frozen=True, slots=True)
class GreyOpenRuntimeParams:
    symbol: str = "HK.01879"
    quantity: int = 1000
    lot_size: int = 100
    max_price: Decimal = Decimal("0")
    max_qty: int = 1000
    max_notional: Decimal = Decimal("0")
    max_order_attempts: int = 3
    cool_down_ms: int = 300
    opening_burst_seconds: float = 0.0
    opening_burst_cool_down_ms: int = 50
    kill_switch_file: Path | None = None
    remark: str = "grey_open_v1"


@dataclass(frozen=True, slots=True)
class CostReducerRuntimeParams:
    cost_reducer_enabled: bool = False
    dry_run_only: bool = True
    manual_approval_required: bool = True
    enable_real_sell: bool = False
    enable_real_rebuy: bool = False
    enable_auto_cost_reducer: bool = False
    core_ratio: Decimal = Decimal("0.5")
    trading_ratio: Decimal = Decimal("0.5")
    max_spread_bps: Decimal = Decimal("20")
    min_turnover_to_activate: Decimal = Decimal("0")
    min_ticks_to_activate: int = 5
    overextension_vol_multiple: Decimal = Decimal("2.0")
    high_pullback_vol_multiple: Decimal = Decimal("0.5")
    rebuy_anchor_vol_band: Decimal = Decimal("1.0")
    estimated_roundtrip_cost_bps: Decimal = Decimal("10")
    safety_buffer_bps: Decimal = Decimal("5")
    max_sell_total_position_ratio: Decimal = Decimal("0.25")
    max_round_trips: int = 1
    max_real_sell_qty: int = 0
    max_real_rebuy_qty: int = 0
    max_real_sell_notional: Decimal = Decimal("0")
    max_real_rebuy_notional: Decimal = Decimal("0")
    max_cost_reducer_orders_per_session: int = 1
    min_seconds_between_cost_reducer_orders: Decimal = Decimal("5")
    require_positive_expected_edge: bool = True
    sell_limit_offset_ticks: int = 0
    rebuy_limit_offset_ticks: int = 0
    min_sell_price: Decimal | None = None
    max_rebuy_price: Decimal | None = None
    max_sell_slippage_bps: Decimal = Decimal("20")
    max_rebuy_slippage_bps: Decimal = Decimal("20")
    min_expected_edge_bps: Decimal = Decimal("0")


@dataclass(slots=True)
class WebUiRuntimeState:
    execution_mode: ExecutionMode = ExecutionMode.LIVE_DRY_RUN
    real_mode_confirmed: bool = False
    active_symbol: str = "HK.01879"
    live_running: bool = False
    event_count: int = 0
    last_error: str | None = None


PRESETS: dict[str, CostReducerRuntimeParams] = {
    "conservative_dry_run": CostReducerRuntimeParams(
        cost_reducer_enabled=True,
        dry_run_only=True,
        manual_approval_required=True,
        max_spread_bps=Decimal("20"),
        max_sell_total_position_ratio=Decimal("0.15"),
        max_round_trips=1,
        min_expected_edge_bps=Decimal("10"),
    ),
    "aggressive_dry_run": CostReducerRuntimeParams(
        cost_reducer_enabled=True,
        dry_run_only=True,
        manual_approval_required=True,
        max_spread_bps=Decimal("100"),
        min_ticks_to_activate=3,
        max_sell_total_position_ratio=Decimal("0.25"),
        max_round_trips=2,
    ),
    "real_buy_only_safe": CostReducerRuntimeParams(
        cost_reducer_enabled=False,
        dry_run_only=True,
        manual_approval_required=True,
        enable_real_sell=False,
        enable_real_rebuy=False,
    ),
    "manual_cost_reducer_safe": CostReducerRuntimeParams(
        cost_reducer_enabled=True,
        dry_run_only=False,
        manual_approval_required=True,
        enable_real_sell=True,
        enable_real_rebuy=True,
        enable_auto_cost_reducer=False,
        max_spread_bps=Decimal("20"),
        max_cost_reducer_orders_per_session=1,
        min_seconds_between_cost_reducer_orders=Decimal("10"),
        min_expected_edge_bps=Decimal("10"),
    ),
}


def cost_reducer_preset(name: str) -> CostReducerRuntimeParams:
    try:
        return PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown cost reducer preset: {name}") from exc


def update_cost_reducer_params(
    params: CostReducerRuntimeParams,
    updates: dict[str, Any],
) -> CostReducerRuntimeParams:
    allowed = set(CostReducerRuntimeParams.__dataclass_fields__)
    converted: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in allowed:
            raise ValueError(f"Unknown cost reducer parameter: {key}")
        converted[key] = _convert_field(CostReducerRuntimeParams, key, value)
    next_params = replace(params, **converted)
    validate_cost_reducer_params(next_params)
    return next_params


def validate_cost_reducer_params(params: CostReducerRuntimeParams) -> None:
    if params.core_ratio <= 0 or params.trading_ratio <= 0:
        raise ValueError("core_ratio and trading_ratio must be positive.")
    if params.max_spread_bps < 0:
        raise ValueError("max_spread_bps must be zero or greater.")
    if params.min_turnover_to_activate < 0:
        raise ValueError("min_turnover_to_activate must be zero or greater.")
    if params.min_ticks_to_activate < 0:
        raise ValueError("min_ticks_to_activate must be zero or greater.")
    if params.max_sell_total_position_ratio < 0 or params.max_sell_total_position_ratio > 1:
        raise ValueError("max_sell_total_position_ratio must be between 0 and 1.")
    if params.max_round_trips < 0:
        raise ValueError("max_round_trips must be zero or greater.")
    if params.max_cost_reducer_orders_per_session < 0:
        raise ValueError("max_cost_reducer_orders_per_session must be zero or greater.")
    if params.min_seconds_between_cost_reducer_orders < 0:
        raise ValueError("min_seconds_between_cost_reducer_orders must be zero or greater.")
    if params.max_sell_slippage_bps < 0 or params.max_rebuy_slippage_bps < 0:
        raise ValueError("slippage caps must be zero or greater.")


def config_to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: config_to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list):
        return [config_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: config_to_jsonable(item) for key, item in value.items()}
    return value


def _convert_field(model: type, key: str, value: Any) -> Any:
    current = model.__dataclass_fields__[key].default
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(float(value))
    if isinstance(current, Decimal) or key in {
        "min_sell_price",
        "max_rebuy_price",
    }:
        if value in {None, ""}:
            return None
        return Decimal(str(value))
    return value
