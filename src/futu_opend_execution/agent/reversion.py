"""Trade-only high-sell/low-rebuy research harness."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from itertools import product
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from futu_opend_execution.agent.newly_listed import (
    DEFAULT_INSTRUMENT_PROFILE,
    build_newly_listed_universe,
)
from futu_opend_execution.data.hshare_l2 import DEFAULT_HSHARE_L2_ROOT
from futu_opend_execution.data.hshare_l2 import HshareL2ReplayProvider
from futu_opend_execution.data.hshare_top_of_book import HshareTopOfBookReplayProvider
from futu_opend_execution.data.market import MarketState


@dataclass(frozen=True, slots=True)
class SellRebuyRules:
    direction: str = "SELL_REBUY"
    entry_vol_multiple: Decimal = Decimal("1.5")
    exit_vol_band: Decimal = Decimal("0.5")
    stop_vol_multiple: Decimal = Decimal("3.0")
    max_hold_states: int = 300
    min_ticks_to_activate: int = 5
    cost_bps: Decimal = Decimal("35")
    quantity: int = 1

    def __post_init__(self) -> None:
        if self.direction not in {"SELL_REBUY", "BUY_SELL"}:
            raise ValueError(f"unsupported direction: {self.direction}")


@dataclass(frozen=True, slots=True)
class SellRebuyGrid:
    direction: tuple[str, ...] = ("SELL_REBUY", "BUY_SELL")
    entry_vol_multiple: tuple[Decimal, ...] = (Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))
    exit_vol_band: tuple[Decimal, ...] = (Decimal("0"), Decimal("0.5"), Decimal("1.0"))
    stop_vol_multiple: tuple[Decimal, ...] = (Decimal("2.0"), Decimal("3.0"))
    max_hold_states: tuple[int, ...] = (60, 300, 900)
    cost_bps: tuple[Decimal, ...] = (Decimal("35"),)

    def iter_rules(self, *, quantity: int = 1, min_ticks_to_activate: int = 5) -> Iterable[SellRebuyRules]:
        keys = tuple(asdict(self))
        for combo in product(*(getattr(self, key) for key in keys)):
            values = dict(zip(keys, combo))
            yield SellRebuyRules(
                quantity=quantity,
                min_ticks_to_activate=min_ticks_to_activate,
                **values,
            )


def optimize_sell_rebuy(
    *,
    market_states: Iterable[MarketState],
    grid: SellRebuyGrid = SellRebuyGrid(),
    top_n: int = 20,
    quantity: int = 1,
    min_ticks_to_activate: int = 5,
) -> dict[str, Any]:
    states = list(market_states)
    results = [evaluate_sell_rebuy(states, rules) for rules in grid.iter_rules(quantity=quantity, min_ticks_to_activate=min_ticks_to_activate)]
    results.sort(key=lambda row: (_decimal(row["net_pnl_after_cost"]), int(row["round_trips_completed"]), -int(row["forced_rebuy_count"])), reverse=True)
    return {
        "event": "sell_rebuy_optimizer_summary",
        "market_state_count": len(states),
        "grid_size": len(results),
        "top_n": min(top_n, len(results)),
        "results": results[:top_n],
    }


def evaluate_sell_rebuy(states: list[MarketState], rules: SellRebuyRules) -> dict[str, Any]:
    open_entry_price: Decimal | None = None
    open_hold_states = 0
    entry_count = 0
    exit_count = 0
    forced_exit_count = 0
    gross_pnl = Decimal("0")
    total_cost = Decimal("0")
    quality_block_count = 0
    last_price: Decimal | None = None

    for state in states:
        if state.book_quality == "BLOCKED" or state.orderbook_limited:
            quality_block_count += 1
        price = state.last_price
        anchor = state.rolling_vwap or state.opening_vwap
        vol = state.realized_vol
        if price is None:
            continue
        last_price = price
        if anchor is None or vol <= 0 or state.tick_count < rules.min_ticks_to_activate:
            continue

        if open_entry_price is None:
            if _entry_signal(price, anchor, vol, rules):
                open_entry_price = price
                open_hold_states = 0
                entry_count += 1
            continue

        open_hold_states += 1
        exit_reason = None
        if _exit_signal(price, anchor, vol, rules):
            exit_reason = "mean_reversion"
        elif _stop_signal(price, open_entry_price, vol, rules):
            exit_reason = "stop_loss"
        elif open_hold_states >= rules.max_hold_states:
            exit_reason = "max_hold"
        if exit_reason:
            gross, cost = _round_trip(open_entry_price, price, rules)
            gross_pnl += gross
            total_cost += cost
            exit_count += 1
            if exit_reason != "mean_reversion":
                forced_exit_count += 1
            open_entry_price = None
            open_hold_states = 0

    open_quantity = 0
    if open_entry_price is not None:
        open_quantity = rules.quantity
        if last_price is not None:
            gross, cost = _round_trip(open_entry_price, last_price, rules)
            gross_pnl += gross
            total_cost += cost
            exit_count += 1
            forced_exit_count += 1
            open_quantity = 0
    net = gross_pnl - total_cost
    sell_count = entry_count if rules.direction == "SELL_REBUY" else exit_count
    rebuy_count = exit_count if rules.direction == "SELL_REBUY" else 0
    buy_count = entry_count if rules.direction == "BUY_SELL" else 0
    return {
        "params": _rules_params(rules),
        "market_state_count": len(states),
        "entry_count": entry_count,
        "exit_count": exit_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "rebuy_count": rebuy_count,
        "round_trips_completed": exit_count,
        "forced_exit_count": forced_exit_count,
        "forced_rebuy_count": forced_exit_count,
        "open_quantity": open_quantity,
        "gross_pnl": str(gross_pnl),
        "estimated_cost": str(total_cost),
        "net_pnl_after_cost": str(net),
        "quality_block_count": quality_block_count,
        "quality_block_ratio": str(_ratio(quality_block_count, len(states))),
        "score": str(net - Decimal(forced_exit_count) * Decimal("0.01")),
    }


def evaluate_newly_listed_sell_rebuy(
    *,
    instrument_profile_path: Path | str = DEFAULT_INSTRUMENT_PROFILE,
    universe_path: Path | str | None = None,
    data_root: Path | str = DEFAULT_HSHARE_L2_ROOT,
    top_of_book_root: Path | str | None = None,
    listing_year: int = 2026,
    dates: Iterable[str] | None = None,
    min_trade_dates: int = 1,
    max_symbols: int | None = None,
    max_dates_per_symbol: int | None = None,
    grid: SellRebuyGrid = SellRebuyGrid(),
    top_n: int = 20,
    validation_days: int = 3,
    min_validation_net_pnl: Decimal | str = Decimal("0"),
    min_validation_round_trips: int = 10,
    max_forced_rebuy_ratio: Decimal | str = Decimal("0.5"),
    max_quality_block_ratio: Decimal | str = Decimal("0.5"),
    quantity: int = 1,
    min_ticks_to_activate: int = 5,
    progress: bool = False,
) -> dict[str, Any]:
    universe = build_newly_listed_universe(
        instrument_profile_path=instrument_profile_path,
        universe_path=universe_path,
        data_root=data_root,
        top_of_book_root=top_of_book_root,
        listing_year=listing_year,
        dates=dates,
        min_trade_dates=min_trade_dates,
        max_symbols=max_symbols,
    )
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(universe["candidates"], start=1):
        symbol = str(candidate["symbol"])
        run_dates = list(candidate["available_trade_dates"])
        if max_dates_per_symbol is not None:
            run_dates = run_dates[-max_dates_per_symbol:]
        for date_index, trade_date in enumerate(run_dates, start=1):
            if progress:
                _emit_progress(symbol, trade_date, candidate_index, universe["candidate_count"], date_index, len(run_dates))
            try:
                states = _market_states(symbol, trade_date, data_root, top_of_book_root)
                summary = optimize_sell_rebuy(market_states=states, grid=grid, top_n=10_000, quantity=quantity, min_ticks_to_activate=min_ticks_to_activate)
                for row in summary["results"]:
                    rows.append({"symbol": symbol, "date": trade_date, "listing_date": candidate["listing_date"], **row})
            except Exception as exc:  # pragma: no cover - operational guardrail
                failures.append({"symbol": symbol, "date": trade_date, "error": repr(exc)})
    return _walk_forward(
        universe=universe,
        rows=rows,
        failures=failures,
        listing_year=listing_year,
        validation_days=validation_days,
        top_n=top_n,
        thresholds={
            "min_validation_net_pnl": str(_decimal(min_validation_net_pnl)),
            "min_validation_round_trips": int(min_validation_round_trips),
            "max_forced_rebuy_ratio": str(_decimal(max_forced_rebuy_ratio)),
            "max_quality_block_ratio": str(_decimal(max_quality_block_ratio)),
        },
    )


def write_sell_rebuy_reports(summary: dict[str, Any], *, json_path: Path | str, markdown_path: Path | str | None = None) -> None:
    target = Path(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if markdown_path is not None:
        md = Path(markdown_path)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(_markdown(summary), encoding="utf-8")


def _walk_forward(
    *,
    universe: dict[str, Any],
    rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    listing_year: int,
    validation_days: int,
    top_n: int,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    all_dates = sorted({str(row["date"]) for row in rows})
    split = max(len(all_dates) - max(validation_days, 1), 0)
    train_dates = all_dates[:split]
    validation_dates = all_dates[split:]
    train = _aggregate([row for row in rows if row["date"] in train_dates])
    validation = _aggregate([row for row in rows if row["date"] in validation_dates])
    validation_by_key = {json.dumps(row["params"], sort_keys=True, ensure_ascii=False): row for row in validation}
    ranking = []
    for train_rank, train_row in enumerate(train, start=1):
        validation_row = validation_by_key.get(json.dumps(train_row["params"], sort_keys=True, ensure_ascii=False), _empty_rollup(train_row["params"]))
        reasons = _candidate_reasons(train_row, thresholds, stage="train")
        reasons.extend(_candidate_reasons(validation_row, thresholds, stage="validation"))
        ranking.append(
            {
                "train_rank": train_rank,
                "candidate_status": "CANDIDATE" if not reasons else "NO_GO",
                "candidate_reasons": reasons,
                "params": train_row["params"],
                "train": train_row,
                "validation": validation_row,
            }
        )
    ranking.sort(key=_rank_key, reverse=True)
    candidates = [row for row in ranking if row["candidate_status"] == "CANDIDATE"]
    return {
        "event": "sell_rebuy_walk_forward_summary",
        "decision": "CANDIDATE" if candidates else "NO_GO",
        "candidate_count": len(candidates),
        "recommended_candidate": candidates[0] if candidates else None,
        "listing_year": listing_year,
        "universe": universe,
        "split": {"all_dates": all_dates, "train_dates": train_dates, "validation_dates": validation_dates, "validation_days": validation_days},
        "candidate_thresholds": thresholds,
        "evaluated_case_count": len({(row["symbol"], row["date"]) for row in rows}),
        "result_row_count": len(rows),
        "failure_count": len(failures),
        "failures": failures,
        "top_n": top_n,
        "walk_forward_ranking": ranking[:top_n],
        "per_case_results": rows,
    }


def _aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = json.dumps(row["params"], sort_keys=True, ensure_ascii=False)
        bucket = buckets.setdefault(
            key,
            {
                "params": row["params"],
                "cases": 0,
                "market_state_count": 0,
                "entry_count": 0,
                "exit_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "rebuy_count": 0,
                "round_trips_completed": 0,
                "forced_exit_count": 0,
                "forced_rebuy_count": 0,
                "open_quantity_sum": 0,
                "gross_pnl_sum": Decimal("0"),
                "estimated_cost_sum": Decimal("0"),
                "net_pnl_after_cost_sum": Decimal("0"),
                "quality_block_count": 0,
            },
        )
        bucket["cases"] += 1
        bucket["market_state_count"] += int(row["market_state_count"])
        bucket["entry_count"] += int(row.get("entry_count", row["sell_count"]))
        bucket["exit_count"] += int(row.get("exit_count", row["round_trips_completed"]))
        bucket["buy_count"] += int(row.get("buy_count", 0))
        bucket["sell_count"] += int(row["sell_count"])
        bucket["rebuy_count"] += int(row["rebuy_count"])
        bucket["round_trips_completed"] += int(row["round_trips_completed"])
        bucket["forced_exit_count"] += int(row.get("forced_exit_count", row["forced_rebuy_count"]))
        bucket["forced_rebuy_count"] += int(row["forced_rebuy_count"])
        bucket["open_quantity_sum"] += int(row["open_quantity"])
        bucket["gross_pnl_sum"] += _decimal(row["gross_pnl"])
        bucket["estimated_cost_sum"] += _decimal(row["estimated_cost"])
        bucket["net_pnl_after_cost_sum"] += _decimal(row["net_pnl_after_cost"])
        bucket["quality_block_count"] += int(row["quality_block_count"])
    output = []
    for item in buckets.values():
        item["quality_block_ratio"] = _ratio(item["quality_block_count"], item["market_state_count"])
        item["forced_rebuy_ratio"] = _ratio(item["forced_rebuy_count"], item["round_trips_completed"])
        output.append(_jsonable(item))
    output.sort(key=lambda row: (_decimal(row["net_pnl_after_cost_sum"]), int(row["round_trips_completed"]), -_decimal(row["forced_rebuy_ratio"])), reverse=True)
    return output


def _candidate_reasons(row: dict[str, Any], thresholds: dict[str, Any], *, stage: str) -> list[str]:
    reasons = []
    if _decimal(row["net_pnl_after_cost_sum"]) <= _decimal(thresholds["min_validation_net_pnl"]):
        reasons.append(f"{stage} net_pnl_after_cost below threshold")
    if int(row["round_trips_completed"]) < int(thresholds["min_validation_round_trips"]):
        reasons.append(f"{stage} completed round trips below threshold")
    if _decimal(row["forced_rebuy_ratio"]) > _decimal(thresholds["max_forced_rebuy_ratio"]):
        reasons.append(f"{stage} forced rebuy ratio above threshold")
    if _decimal(row["quality_block_ratio"]) > _decimal(thresholds["max_quality_block_ratio"]):
        reasons.append(f"{stage} quality block ratio above threshold")
    return reasons


def _rank_key(row: dict[str, Any]) -> tuple[int, Decimal, int, Decimal, Decimal]:
    validation = row["validation"]
    return (
        1 if row["candidate_status"] == "CANDIDATE" else 0,
        _decimal(validation["net_pnl_after_cost_sum"]),
        int(validation["round_trips_completed"]),
        -_decimal(validation["forced_rebuy_ratio"]),
        -_decimal(validation["quality_block_ratio"]),
    )


def _empty_rollup(params: dict[str, Any]) -> dict[str, Any]:
    return _jsonable(
        {
            "params": params,
            "cases": 0,
            "market_state_count": 0,
            "entry_count": 0,
            "exit_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "rebuy_count": 0,
            "round_trips_completed": 0,
            "forced_exit_count": 0,
            "forced_rebuy_count": 0,
            "open_quantity_sum": 0,
            "gross_pnl_sum": Decimal("0"),
            "estimated_cost_sum": Decimal("0"),
            "net_pnl_after_cost_sum": Decimal("0"),
            "quality_block_count": 0,
            "quality_block_ratio": Decimal("0"),
            "forced_rebuy_ratio": Decimal("0"),
        }
    )


def _market_states(symbol: str, trade_date: str, data_root: Path | str, top_of_book_root: Path | str | None) -> list[MarketState]:
    if top_of_book_root:
        provider = HshareTopOfBookReplayProvider(data_root=top_of_book_root, dates=[trade_date], symbols=[symbol])
    else:
        provider = HshareL2ReplayProvider(data_root=data_root, dates=[trade_date], symbols=[symbol])
    return list(provider.iter_market_states())


def _round_trip(sell_price: Decimal, rebuy_price: Decimal, rules: SellRebuyRules) -> tuple[Decimal, Decimal]:
    qty = Decimal(rules.quantity)
    gross = (sell_price - rebuy_price) * qty if rules.direction == "SELL_REBUY" else (rebuy_price - sell_price) * qty
    cost = (sell_price + rebuy_price) * qty * rules.cost_bps / Decimal("10000")
    return gross, cost


def _entry_signal(price: Decimal, anchor: Decimal, vol: Decimal, rules: SellRebuyRules) -> bool:
    if rules.direction == "BUY_SELL":
        return price <= anchor - rules.entry_vol_multiple * vol
    return price >= anchor + rules.entry_vol_multiple * vol


def _exit_signal(price: Decimal, anchor: Decimal, vol: Decimal, rules: SellRebuyRules) -> bool:
    if rules.direction == "BUY_SELL":
        return price >= anchor - rules.exit_vol_band * vol
    return price <= anchor + rules.exit_vol_band * vol


def _stop_signal(price: Decimal, entry_price: Decimal, vol: Decimal, rules: SellRebuyRules) -> bool:
    if rules.direction == "BUY_SELL":
        return price <= entry_price - rules.stop_vol_multiple * vol
    return price >= entry_price + rules.stop_vol_multiple * vol


def _rules_params(rules: SellRebuyRules) -> dict[str, Any]:
    return {
        "direction": rules.direction,
        "entry_vol_multiple": str(rules.entry_vol_multiple),
        "exit_vol_band": str(rules.exit_vol_band),
        "stop_vol_multiple": str(rules.stop_vol_multiple),
        "max_hold_states": rules.max_hold_states,
        "cost_bps": str(rules.cost_bps),
        "quantity": rules.quantity,
        "min_ticks_to_activate": rules.min_ticks_to_activate,
    }


def _jsonable(item: dict[str, Any]) -> dict[str, Any]:
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in item.items()}


def _ratio(numerator: Any, denominator: Any) -> Decimal:
    bottom = _decimal(denominator)
    if bottom <= 0:
        return Decimal("0")
    return (_decimal(numerator) / bottom).quantize(Decimal("0.000001"))


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Sell/Rebuy Reversion Walk-Forward",
        "",
        f"- decision: {summary['decision']}",
        f"- candidate_count: {summary['candidate_count']}",
        f"- universe_count: {summary['universe']['candidate_count']}",
        f"- evaluated_case_count: {summary['evaluated_case_count']}",
        f"- result_row_count: {summary['result_row_count']}",
        f"- failure_count: {summary['failure_count']}",
        f"- candidate_thresholds: `{summary['candidate_thresholds']}`",
        "",
        "| rank | status | validation_net_pnl | round_trips | forced_ratio | quality_ratio | reasons | params |",
        "| ---: | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for index, row in enumerate(summary["walk_forward_ranking"], start=1):
        validation = row["validation"]
        reasons = "; ".join(row["candidate_reasons"]) or "passed"
        lines.append(
            f"| {index} | {row['candidate_status']} | {validation['net_pnl_after_cost_sum']} | {validation['round_trips_completed']} | {validation['forced_rebuy_ratio']} | {validation['quality_block_ratio']} | {reasons} | `{row['params']}` |"
        )
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- This is sell-first/rebuy-later replay research for existing inventory, not a real-trading recommendation.",
            "- Forced rebuys close risk at stop, max hold, or session end and are penalized in candidate checks.",
            "- High quality-block ratios mean the signal is not execution-grade under the current Hshare handoff.",
        ]
    )
    return "\n".join(lines) + "\n"


def _emit_progress(symbol: str, trade_date: str, candidate_index: int, candidate_count: int, date_index: int, date_count: int) -> None:
    print(
        json.dumps(
            {
                "event": "sell_rebuy_progress",
                "symbol": symbol,
                "date": trade_date,
                "candidate_index": candidate_index,
                "candidate_count": candidate_count,
                "date_index": date_index,
                "date_count": date_count,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
