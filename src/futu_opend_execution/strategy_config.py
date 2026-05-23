"""Runtime config models for the OpenD Trading Agent."""

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
class AgentRuntimeParams:
    symbol: str = "HK.00700"
    current_qty: int = 0
    cost_price: Decimal = Decimal("0")
    lot_size: int = 100
    log_dir: Path = Path("logs/agent")
    report_dir: Path = Path("reports/agent")
    kill_switch_file: Path = Path("logs/agent/KILL_SWITCH")


@dataclass(frozen=True, slots=True)
class CostReducerRuntimeParams:
    enabled: bool = True
    dry_run_only: bool = True
    manual_approval_required: bool = True
    enable_real_sell: bool = False
    enable_real_rebuy: bool = False
    enable_auto_cost_reducer: bool = False
    max_spread_bps: Decimal = Decimal("20")
    min_turnover_to_activate: Decimal = Decimal("0")
    min_ticks_to_activate: int = 5
    overextension_vol_multiple: Decimal = Decimal("2.0")
    high_pullback_vol_multiple: Decimal = Decimal("0.5")
    rebuy_anchor_vol_band: Decimal = Decimal("1.0")
    estimated_roundtrip_cost_bps: Decimal = Decimal("35")
    safety_buffer_bps: Decimal = Decimal("20")
    max_sell_total_position_ratio: Decimal = Decimal("0.25")
    max_round_trips: int = 1
    max_real_sell_qty: int = 0
    max_real_rebuy_qty: int = 0
    max_real_notional: Decimal = Decimal("0")
    max_cost_reducer_orders_per_session: int = 1
    min_seconds_between_cost_reducer_orders: Decimal = Decimal("5")
    require_positive_expected_edge: bool = True
    sell_limit_offset_ticks: int = 0
    rebuy_limit_offset_ticks: int = 0
    max_sell_slippage_bps: Decimal = Decimal("20")
    max_rebuy_slippage_bps: Decimal = Decimal("20")
    min_expected_edge_bps: Decimal = Decimal("0")


PRESETS: dict[str, CostReducerRuntimeParams] = {
    "conservative_dry_run": CostReducerRuntimeParams(max_spread_bps=Decimal("20"), max_sell_total_position_ratio=Decimal("0.15")),
    "aggressive_dry_run": CostReducerRuntimeParams(max_spread_bps=Decimal("100"), min_ticks_to_activate=3, max_round_trips=2),
    "manual_cost_reducer_safe": CostReducerRuntimeParams(
        dry_run_only=False,
        manual_approval_required=True,
        enable_real_sell=True,
        enable_real_rebuy=True,
        enable_auto_cost_reducer=False,
        max_spread_bps=Decimal("20"),
        max_cost_reducer_orders_per_session=1,
    ),
}


def update_cost_reducer_params(params: CostReducerRuntimeParams, updates: dict[str, Any]) -> CostReducerRuntimeParams:
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
    if params.max_spread_bps < 0:
        raise ValueError("max_spread_bps must be non-negative")
    if params.min_turnover_to_activate < 0 or params.min_ticks_to_activate < 0:
        raise ValueError("activation thresholds must be non-negative")
    if not Decimal("0") <= params.max_sell_total_position_ratio <= Decimal("1"):
        raise ValueError("max_sell_total_position_ratio must be between 0 and 1")
    if params.max_round_trips < 0:
        raise ValueError("max_round_trips must be non-negative")
    if params.enable_auto_cost_reducer:
        raise ValueError("LIVE_REAL_COST_REDUCER_AUTO is experimental and disabled by default")


def config_to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: config_to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _convert_field(model: type, key: str, value: Any) -> Any:
    current = model.__dataclass_fields__[key].default
    if isinstance(current, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(float(value))
    if isinstance(current, Decimal):
        return Decimal(str(value))
    return value
