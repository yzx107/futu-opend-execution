"""Small grid-search harness for existing-position cost reducer rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from itertools import product
from pathlib import Path
import json
from typing import Iterable

from futu_opend_execution.agent.runtime import (
    TradingAgentConfig,
    build_inventory_for_existing_position,
    default_strategy,
)
from futu_opend_execution.data.market import MarketState
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerDecision,
    CostReducerExecutableStatus,
    CostReducerExecutionPolicy,
    CostReducerRules,
    CostReducerState,
    apply_dry_run_fill,
)
from futu_opend_execution.strategies.cost_reducer import CostReducerStrategy


@dataclass(frozen=True, slots=True)
class CostReducerGrid:
    overextension_vol_multiple: tuple[Decimal, ...] = (Decimal("1.5"), Decimal("2.0"), Decimal("2.5"))
    high_pullback_vol_multiple: tuple[Decimal, ...] = (Decimal("0.3"), Decimal("0.5"), Decimal("0.8"))
    rebuy_anchor_vol_band: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("1.0"))
    safety_buffer_bps: tuple[Decimal, ...] = (Decimal("20"), Decimal("30"))
    max_sell_total_position_ratio: tuple[Decimal, ...] = (Decimal("0.1"), Decimal("0.25"))
    max_round_trips: tuple[int, ...] = (1,)

    def iter_rule_overrides(self) -> Iterable[dict[str, object]]:
        keys = tuple(asdict(self))
        values = tuple(getattr(self, key) for key in keys)
        for combo in product(*values):
            yield dict(zip(keys, combo))


def optimize_cost_reducer(
    *,
    config: TradingAgentConfig,
    market_states: Iterable[MarketState],
    grid: CostReducerGrid = CostReducerGrid(),
    top_n: int = 20,
) -> dict[str, object]:
    states = list(market_states)
    results = [_evaluate_combo(config=config, market_states=states, overrides=overrides) for overrides in grid.iter_rule_overrides()]
    results.sort(key=_rank_key, reverse=True)
    return {
        "event": "optimizer_summary",
        "symbol": config.symbol,
        "market_state_count": len(states),
        "grid_size": len(results),
        "top_n": min(top_n, len(results)),
        "results": results[:top_n],
    }


def write_optimizer_reports(summary: dict[str, object], *, json_path: Path | str, markdown_path: Path | str | None = None) -> None:
    target = Path(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if markdown_path is not None:
        md = Path(markdown_path)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(_markdown(summary), encoding="utf-8")


def _evaluate_combo(
    *,
    config: TradingAgentConfig,
    market_states: list[MarketState],
    overrides: dict[str, object],
) -> dict[str, object]:
    combo_config = replace(
        config,
        max_sell_total_position_ratio=overrides["max_sell_total_position_ratio"],
        max_round_trips=int(overrides["max_round_trips"]),
    )
    inventory = build_inventory_for_existing_position(combo_config)
    initial_cost_basis = inventory.economic_cost_basis
    state = CostReducerState()
    strategy = _strategy_for(combo_config, overrides)
    sell_count = 0
    rebuy_count = 0
    risk_block_count = 0
    quality_block_count = 0
    open_sells: list[tuple[int, Decimal]] = []
    realized_net_pnl = Decimal("0")

    for market in market_states:
        intent = strategy.evaluate(market=market, inventory=inventory, state=state)
        if intent.status is CostReducerExecutableStatus.RISK_BLOCKED:
            risk_block_count += 1
            if "book quality" in intent.reason or "book depth" in intent.reason:
                quality_block_count += 1
        if intent.status is not CostReducerExecutableStatus.DRY_RUN_SIGNAL or intent.limit_price is None:
            continue

        if intent.action is CostReducerAction.SELL_TRADING:
            sell_count += 1
            open_sells.append((intent.quantity, intent.limit_price))
        elif intent.action is CostReducerAction.REBUY_TRADING:
            rebuy_count += 1
            realized_net_pnl += _match_rebuy(open_sells, intent.quantity, intent.limit_price, strategy.rules.estimated_roundtrip_cost_bps)

        apply_dry_run_fill(
            decision=CostReducerDecision(intent.action, quantity=intent.quantity, reason=intent.reason),
            market=_adaptive_like(market),
            inventory=inventory,
            state=state,
            estimated_roundtrip_cost_bps=strategy.rules.estimated_roundtrip_cost_bps,
        )

    cost_basis_reduction = initial_cost_basis - inventory.economic_cost_basis
    open_quantity = sum(quantity for quantity, _ in open_sells)
    open_quantity_penalty = Decimal(open_quantity) * Decimal("0.01")
    return {
        "params": _jsonable_params(overrides),
        "market_state_count": len(market_states),
        "sell_count": sell_count,
        "rebuy_count": rebuy_count,
        "round_trips_completed": state.round_trips_completed,
        "open_quantity": open_quantity,
        "open_quantity_penalty": str(open_quantity_penalty),
        "realized_net_pnl": str(realized_net_pnl),
        "net_pnl_after_cost": str(realized_net_pnl),
        "initial_cost_basis": str(initial_cost_basis),
        "final_economic_cost_basis": str(inventory.economic_cost_basis),
        "cost_basis_reduction": str(cost_basis_reduction),
        "risk_block_count": risk_block_count,
        "quality_block_count": quality_block_count,
        "score": str(_score(cost_basis_reduction, realized_net_pnl, open_quantity)),
    }


def _strategy_for(config: TradingAgentConfig, overrides: dict[str, object]) -> CostReducerStrategy:
    base = default_strategy(config)
    rules = replace(
        base.rules,
        overextension_vol_multiple=_decimal(overrides["overextension_vol_multiple"]),
        high_pullback_vol_multiple=_decimal(overrides["high_pullback_vol_multiple"]),
        rebuy_anchor_vol_band=_decimal(overrides["rebuy_anchor_vol_band"]),
        safety_buffer_bps=_decimal(overrides["safety_buffer_bps"]),
        max_sell_total_position_ratio=_decimal(overrides["max_sell_total_position_ratio"]),
        max_round_trips=int(overrides["max_round_trips"]),
    )
    policy = replace(CostReducerExecutionPolicy(), dry_run_only=True)
    return CostReducerStrategy(rules=rules, policy=policy)


def _match_rebuy(open_sells: list[tuple[int, Decimal]], quantity: int, rebuy_price: Decimal, cost_bps: Decimal) -> Decimal:
    remaining = quantity
    net = Decimal("0")
    while remaining > 0 and open_sells:
        sell_qty, sell_price = open_sells[0]
        matched = min(remaining, sell_qty)
        gross = (sell_price - rebuy_price) * Decimal(matched)
        cost = (sell_price + rebuy_price) * Decimal(matched) * cost_bps / Decimal("10000")
        net += gross - cost
        sell_qty -= matched
        remaining -= matched
        if sell_qty:
            open_sells[0] = (sell_qty, sell_price)
        else:
            open_sells.pop(0)
    return net


def _score(cost_basis_reduction: Decimal, realized_net_pnl: Decimal, open_quantity: int) -> Decimal:
    return realized_net_pnl + cost_basis_reduction - Decimal(open_quantity) * Decimal("0.01")


def _rank_key(row: dict[str, object]) -> tuple[Decimal, int, Decimal]:
    return (
        _decimal(row["score"]),
        int(row["round_trips_completed"]),
        _decimal(row["realized_net_pnl"]),
    )


def _markdown(summary: dict[str, object]) -> str:
    rows = summary.get("results", [])
    lines = [
        f"# Cost Reducer Optimizer {summary.get('symbol')}",
        "",
        f"- market_state_count: {summary.get('market_state_count')}",
        f"- grid_size: {summary.get('grid_size')}",
        "",
        "| rank | score | sells | rebuys | open_qty | net_pnl | cost_basis_reduction | params |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for index, row in enumerate(rows, start=1):
        lines.append(
            "| {rank} | {score} | {sell_count} | {rebuy_count} | {open_quantity} | {realized_net_pnl} | {cost_basis_reduction} | `{params}` |".format(
                rank=index,
                **row,
            )
        )
    return "\n".join(lines) + "\n"


def _jsonable_params(params: dict[str, object]) -> dict[str, object]:
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in params.items()}


def _adaptive_like(market: MarketState):
    from futu_opend_execution.agent.runtime import _adaptive_like as convert

    return convert(market)


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
