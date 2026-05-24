"""Feature/label harness for newly listed HK replay research."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from futu_opend_execution.agent.newly_listed import (
    DEFAULT_INSTRUMENT_PROFILE,
    build_newly_listed_universe,
)
from futu_opend_execution.data.hshare_l2 import DEFAULT_HSHARE_L2_ROOT, HshareL2ReplayProvider
from futu_opend_execution.data.hshare_top_of_book import HshareTopOfBookReplayProvider
from futu_opend_execution.data.market import MarketState


@dataclass(frozen=True, slots=True)
class FeatureLabelRules:
    horizons_seconds: tuple[int, ...] = (30, 60, 300)
    cost_bps: Decimal = Decimal("35")
    min_edge_bps: Decimal = Decimal("0")
    min_group_count: int = 30
    min_group_symbols: int = 2
    min_hit_rate: Decimal = Decimal("0.55")
    min_avg_edge_bps: Decimal = Decimal("0")
    max_rows_per_case: int | None = None


def build_feature_label_rows(
    states: Iterable[MarketState],
    *,
    symbol: str,
    trade_date: str,
    listing_date: str | None = None,
    rules: FeatureLabelRules = FeatureLabelRules(),
) -> list[dict[str, Any]]:
    ordered = sorted(states, key=lambda item: item.timestamp)
    if not ordered:
        return []
    first_timestamp = ordered[0].timestamp
    output: list[dict[str, Any]] = []
    for index, state in enumerate(ordered):
        if rules.max_rows_per_case is not None and len(output) >= rules.max_rows_per_case:
            break
        if state.last_price is None or state.last_price <= 0:
            continue
        row = _feature_row(state, symbol=symbol, trade_date=trade_date, listing_date=listing_date, first_timestamp=first_timestamp)
        row["labels"] = _labels_for_state(ordered, index, rules)
        output.append(row)
    return output


def evaluate_newly_listed_feature_labels(
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
    rules: FeatureLabelRules = FeatureLabelRules(),
    top_n: int = 20,
    keep_rows: bool = False,
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
    failures: list[dict[str, str]] = []
    evaluated_cases = 0
    for candidate_index, candidate in enumerate(universe["candidates"], start=1):
        symbol = str(candidate["symbol"])
        run_dates = list(candidate["available_trade_dates"])
        if max_dates_per_symbol is not None:
            run_dates = run_dates[-max_dates_per_symbol:]
        for date_index, trade_date in enumerate(run_dates, start=1):
            if progress:
                _emit_progress(symbol, trade_date, candidate_index, universe["candidate_count"], date_index, len(run_dates))
            try:
                case_rows = build_feature_label_rows(
                    _market_states(symbol, trade_date, data_root, top_of_book_root),
                    symbol=symbol,
                    trade_date=trade_date,
                    listing_date=candidate.get("listing_date"),
                    rules=rules,
                )
                rows.extend(case_rows)
                evaluated_cases += 1
            except Exception as exc:  # pragma: no cover - operational guardrail
                failures.append({"symbol": symbol, "date": str(trade_date), "error": repr(exc)})
    ranking = _rank_feature_groups(rows, rules)[:top_n]
    candidates = [row for row in ranking if row["candidate_status"] == "CANDIDATE"]
    summary: dict[str, Any] = {
        "event": "newly_listed_feature_label_summary",
        "decision": "CANDIDATE" if candidates else "NO_GO",
        "candidate_count": len(candidates),
        "recommended_candidate": candidates[0] if candidates else None,
        "listing_year": listing_year,
        "universe": universe,
        "rules": _rules_jsonable(rules),
        "evaluated_case_count": evaluated_cases,
        "feature_row_count": len(rows),
        "quality_ok_row_count": sum(1 for row in rows if row["quality_ok"]),
        "failure_count": len(failures),
        "failures": failures,
        "top_n": top_n,
        "group_ranking": ranking,
        "caveats": [
            "feature labels use future replay prices and are for research only",
            "SELL_REBUY assumes existing inventory can sell at current best bid and rebuy at a future best ask",
            "BUY_SELL is paper/replay research only and is not connected to real-order execution",
            "positive labels are not fill guarantees; depth, latency, order priority, and fees remain simplified",
        ],
    }
    if keep_rows:
        summary["feature_label_rows"] = rows
    return summary


def write_feature_label_reports(
    summary: dict[str, Any],
    *,
    json_path: Path | str,
    markdown_path: Path | str | None = None,
    rows_jsonl_path: Path | str | None = None,
) -> None:
    rows = list(summary.get("feature_label_rows", []))
    public = {key: value for key, value in summary.items() if key != "feature_label_rows"}
    target = Path(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(public, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if markdown_path is not None:
        md = Path(markdown_path)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(_markdown(public), encoding="utf-8")
    if rows_jsonl_path is not None:
        rows_path = Path(rows_jsonl_path)
        rows_path.parent.mkdir(parents=True, exist_ok=True)
        rows_path.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")


def _feature_row(
    state: MarketState,
    *,
    symbol: str,
    trade_date: str,
    listing_date: str | None,
    first_timestamp,
) -> dict[str, Any]:
    anchor = state.rolling_vwap or state.opening_vwap
    z = _zscore(state.last_price, anchor, state.realized_vol)
    return {
        "event": "feature_label_row",
        "symbol": symbol,
        "date": trade_date,
        "listing_date": listing_date,
        "days_since_listing": _days_between(listing_date, trade_date),
        "timestamp": state.timestamp.isoformat(),
        "seconds_since_first_state": int((state.timestamp - first_timestamp).total_seconds()),
        "last_price": _str(state.last_price),
        "best_bid": _str(state.best_bid),
        "best_ask": _str(state.best_ask),
        "bid_size": _str(state.bid_size),
        "ask_size": _str(state.ask_size),
        "spread_bps": _str(state.spread_bps),
        "orderbook_imbalance": _str(state.orderbook_imbalance),
        "opening_vwap": _str(state.opening_vwap),
        "rolling_vwap": _str(state.rolling_vwap),
        "realized_vol": _str(state.realized_vol),
        "price_anchor_bps": _str(_bps(state.last_price, anchor)),
        "price_anchor_z": _str(z),
        "high_pullback_vol": _str(_vol_distance(state.rolling_high, state.last_price, state.realized_vol)),
        "low_rebound_vol": _str(_vol_distance(state.last_price, state.rolling_low, state.realized_vol)),
        "volume_delta": _str(state.volume_delta),
        "turnover_delta": _str(state.turnover_delta),
        "tick_count": state.tick_count,
        "quality_ok": _quality_ok(state),
        "quality_reason": _quality_reason(state),
        "book_quality": state.book_quality,
    }


def _labels_for_state(states: list[MarketState], index: int, rules: FeatureLabelRules) -> dict[str, dict[str, Any]]:
    state = states[index]
    labels: dict[str, dict[str, Any]] = {}
    for horizon in rules.horizons_seconds:
        future = _future_window(states, index, horizon)
        labels[str(horizon)] = _horizon_labels(state, future, rules.cost_bps)
    return labels


def _horizon_labels(state: MarketState, future: list[MarketState], cost_bps: Decimal) -> dict[str, Any]:
    sell_price = _positive(state.best_bid) or _positive(state.last_price)
    buy_price = _positive(state.best_ask) or _positive(state.last_price)
    future_asks = [_positive(item.best_ask) or _positive(item.last_price) for item in future]
    future_bids = [_positive(item.best_bid) or _positive(item.last_price) for item in future]
    future_asks = [value for value in future_asks if value is not None]
    future_bids = [value for value in future_bids if value is not None]
    min_future_ask = min(future_asks) if future_asks else None
    max_future_ask = max(future_asks) if future_asks else None
    min_future_bid = min(future_bids) if future_bids else None
    max_future_bid = max(future_bids) if future_bids else None
    sell_edge = _sell_rebuy_edge_bps(sell_price, min_future_ask, cost_bps)
    buy_edge = _net_edge_bps(max_future_bid, buy_price, cost_bps)
    return {
        "future_state_count": len(future),
        "min_future_ask": _str(min_future_ask),
        "max_future_bid": _str(max_future_bid),
        "sell_rebuy_edge_bps": _str(sell_edge),
        "buy_sell_edge_bps": _str(buy_edge),
        "sell_rebuy_adverse_bps": _str(_bps(max_future_ask, sell_price)),
        "buy_sell_adverse_bps": _str(_bps(buy_price, min_future_bid)),
    }


def _future_window(states: list[MarketState], index: int, horizon_seconds: int) -> list[MarketState]:
    start = states[index].timestamp
    end = start.timestamp() + horizon_seconds
    output = []
    for item in states[index + 1 :]:
        if item.timestamp.timestamp() > end:
            break
        output.append(item)
    return output


def _rank_feature_groups(rows: list[dict[str, Any]], rules: FeatureLabelRules) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        for horizon, label in row["labels"].items():
            for direction, edge_key in (("SELL_REBUY", "sell_rebuy_edge_bps"), ("BUY_SELL", "buy_sell_edge_bps")):
                edge = _decimal(label.get(edge_key))
                if edge is None:
                    continue
                key = _group_key(row, horizon, direction)
                group = groups.setdefault(key, _empty_group(row, horizon, direction))
                group["count"] += 1
                group["edge_sum"] += edge
                group["positive_count"] += int(edge > rules.min_edge_bps)
                group["symbols"].add(row["symbol"])
                group["dates"].add(row["date"])
                group["quality_ok_count"] += int(row["quality_ok"])
                group["max_edge_bps"] = max(group["max_edge_bps"], edge)
                group["min_edge_bps"] = min(group["min_edge_bps"], edge)
    ranking = [_finalize_group(group, rules) for group in groups.values()]
    ranking.sort(
        key=lambda item: (
            1 if item["candidate_status"] == "CANDIDATE" else 0,
            _decimal(item["avg_edge_bps"]) or Decimal("-999999"),
            _decimal(item["hit_rate"]) or Decimal("0"),
            int(item["count"]),
        ),
        reverse=True,
    )
    return ranking


def _empty_group(row: dict[str, Any], horizon: str, direction: str) -> dict[str, Any]:
    return {
        "horizon_seconds": int(horizon),
        "direction": direction,
        "price_anchor_z_bucket": _z_bucket(row["price_anchor_z"]),
        "imbalance_bucket": _imbalance_bucket(row["orderbook_imbalance"]),
        "spread_bucket": _spread_bucket(row["spread_bps"]),
        "pullback_bucket": _pullback_bucket(row, direction),
        "quality_ok": bool(row["quality_ok"]),
        "count": 0,
        "positive_count": 0,
        "edge_sum": Decimal("0"),
        "max_edge_bps": Decimal("-999999"),
        "min_edge_bps": Decimal("999999"),
        "quality_ok_count": 0,
        "symbols": set(),
        "dates": set(),
    }


def _finalize_group(group: dict[str, Any], rules: FeatureLabelRules) -> dict[str, Any]:
    count = int(group["count"])
    avg = group["edge_sum"] / Decimal(count) if count else Decimal("0")
    hit_rate = Decimal(group["positive_count"]) / Decimal(count) if count else Decimal("0")
    symbol_count = len(group["symbols"])
    reasons = []
    if not group["quality_ok"]:
        reasons.append("quality bucket is not OK")
    if count < rules.min_group_count:
        reasons.append("sample count below threshold")
    if symbol_count < rules.min_group_symbols:
        reasons.append("symbol count below threshold")
    if hit_rate < rules.min_hit_rate:
        reasons.append("hit rate below threshold")
    if avg <= rules.min_avg_edge_bps:
        reasons.append("average edge below threshold")
    return {
        "candidate_status": "CANDIDATE" if not reasons else "NO_GO",
        "candidate_reasons": reasons,
        "horizon_seconds": group["horizon_seconds"],
        "direction": group["direction"],
        "price_anchor_z_bucket": group["price_anchor_z_bucket"],
        "imbalance_bucket": group["imbalance_bucket"],
        "spread_bucket": group["spread_bucket"],
        "pullback_bucket": group["pullback_bucket"],
        "quality_ok": group["quality_ok"],
        "count": count,
        "positive_count": group["positive_count"],
        "hit_rate": str(hit_rate.quantize(Decimal("0.000001"))),
        "avg_edge_bps": str(avg.quantize(Decimal("0.000001"))),
        "max_edge_bps": str(group["max_edge_bps"]),
        "min_edge_bps": str(group["min_edge_bps"]),
        "quality_ok_count": group["quality_ok_count"],
        "symbol_count": symbol_count,
        "date_count": len(group["dates"]),
    }


def _group_key(row: dict[str, Any], horizon: str, direction: str) -> tuple[Any, ...]:
    return (
        horizon,
        direction,
        _z_bucket(row["price_anchor_z"]),
        _imbalance_bucket(row["orderbook_imbalance"]),
        _spread_bucket(row["spread_bps"]),
        _pullback_bucket(row, direction),
        bool(row["quality_ok"]),
    )


def _market_states(symbol: str, trade_date: str, data_root: Path | str, top_of_book_root: Path | str | None) -> list[MarketState]:
    if top_of_book_root:
        provider = HshareTopOfBookReplayProvider(data_root=top_of_book_root, dates=[trade_date], symbols=[symbol])
    else:
        provider = HshareL2ReplayProvider(data_root=data_root, dates=[trade_date], symbols=[symbol])
    return list(provider.iter_market_states())


def _quality_ok(state: MarketState) -> bool:
    return not (
        state.book_quality == "BLOCKED"
        or state.orderbook_limited
        or state.book_crossed
        or state.book_residue
        or state.book_window_excluded
        or state.same_millisecond_batch_risk
        or state.book_depth_limited
    )


def _quality_reason(state: MarketState) -> str:
    reasons = []
    if state.book_quality == "BLOCKED":
        reasons.append("book_quality_blocked")
    if state.orderbook_limited:
        reasons.append("orderbook_limited")
    if state.book_crossed:
        reasons.append("book_crossed")
    if state.book_residue:
        reasons.append("book_residue")
    if state.book_window_excluded:
        reasons.append("book_window_excluded")
    if state.same_millisecond_batch_risk:
        reasons.append("same_millisecond_batch_risk")
    if state.book_depth_limited:
        reasons.append("book_depth_limited")
    return ",".join(reasons) or "OK"


def _z_bucket(value: Any) -> str:
    z = _decimal(value)
    if z is None:
        return "z:na"
    if z >= 4:
        return "z>=4"
    if z >= 3:
        return "3<=z<4"
    if z >= 2:
        return "2<=z<3"
    if z <= -4:
        return "z<=-4"
    if z <= -3:
        return "-4<z<=-3"
    if z <= -2:
        return "-3<z<=-2"
    return "-2<z<2"


def _imbalance_bucket(value: Any) -> str:
    imbalance = _decimal(value)
    if imbalance is None:
        return "imbalance:na"
    if imbalance >= Decimal("0.2"):
        return "buy_pressure"
    if imbalance <= Decimal("-0.2"):
        return "sell_pressure"
    return "neutral"


def _spread_bucket(value: Any) -> str:
    spread = _decimal(value)
    if spread is None:
        return "spread:na"
    if spread <= 20:
        return "spread<=20"
    if spread <= 50:
        return "20<spread<=50"
    return "spread>50"


def _pullback_bucket(row: dict[str, Any], direction: str) -> str:
    key = "high_pullback_vol" if direction == "SELL_REBUY" else "low_rebound_vol"
    value = _decimal(row.get(key))
    if value is None:
        return "pullback:na"
    if value >= 2:
        return "pullback>=2"
    if value >= 1:
        return "1<=pullback<2"
    return "pullback<1"


def _net_edge_bps(exit_price: Decimal | None, entry_price: Decimal | None, cost_bps: Decimal) -> Decimal | None:
    gross = _bps(exit_price, entry_price)
    if gross is None:
        return None
    return gross - cost_bps


def _sell_rebuy_edge_bps(sell_price: Decimal | None, rebuy_price: Decimal | None, cost_bps: Decimal) -> Decimal | None:
    if sell_price is None or rebuy_price is None or sell_price <= 0:
        return None
    return ((sell_price - rebuy_price) / sell_price * Decimal("10000")).quantize(Decimal("0.000001")) - cost_bps


def _bps(top: Decimal | None, bottom: Decimal | None) -> Decimal | None:
    if top is None or bottom is None or bottom <= 0:
        return None
    return ((top - bottom) / bottom * Decimal("10000")).quantize(Decimal("0.000001"))


def _zscore(price: Decimal | None, anchor: Decimal | None, vol: Decimal) -> Decimal | None:
    if price is None or anchor is None or vol <= 0:
        return None
    return ((price - anchor) / vol).quantize(Decimal("0.000001"))


def _vol_distance(top: Decimal | None, bottom: Decimal | None, vol: Decimal) -> Decimal | None:
    if top is None or bottom is None or vol <= 0:
        return None
    return ((top - bottom) / vol).quantize(Decimal("0.000001"))


def _positive(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None


def _decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _days_between(start: str | None, end: str) -> int | None:
    if not start:
        return None
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _rules_jsonable(rules: FeatureLabelRules) -> dict[str, Any]:
    return {
        "horizons_seconds": list(rules.horizons_seconds),
        "cost_bps": str(rules.cost_bps),
        "min_edge_bps": str(rules.min_edge_bps),
        "min_group_count": rules.min_group_count,
        "min_group_symbols": rules.min_group_symbols,
        "min_hit_rate": str(rules.min_hit_rate),
        "min_avg_edge_bps": str(rules.min_avg_edge_bps),
        "max_rows_per_case": rules.max_rows_per_case,
    }


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Newly Listed Feature Labels",
        "",
        f"- decision: {summary['decision']}",
        f"- candidate_count: {summary['candidate_count']}",
        f"- universe_count: {summary['universe']['candidate_count']}",
        f"- evaluated_case_count: {summary['evaluated_case_count']}",
        f"- feature_row_count: {summary['feature_row_count']}",
        f"- quality_ok_row_count: {summary['quality_ok_row_count']}",
        f"- failure_count: {summary['failure_count']}",
        f"- rules: `{summary['rules']}`",
        "",
        "| rank | status | horizon | direction | avg_edge_bps | hit_rate | count | symbols | z | imbalance | spread | pullback | reasons |",
        "| ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(summary["group_ranking"], start=1):
        reasons = "; ".join(row["candidate_reasons"]) or "passed"
        lines.append(
            f"| {index} | {row['candidate_status']} | {row['horizon_seconds']} | {row['direction']} | {row['avg_edge_bps']} | {row['hit_rate']} | {row['count']} | {row['symbol_count']} | {row['price_anchor_z_bucket']} | {row['imbalance_bucket']} | {row['spread_bucket']} | {row['pullback_bucket']} | {reasons} |"
        )
    lines.extend(["", "## Caveats", ""])
    lines.extend(f"- {item}" for item in summary["caveats"])
    return "\n".join(lines) + "\n"


def _emit_progress(symbol: str, trade_date: str, candidate_index: int, candidate_count: int, date_index: int, date_count: int) -> None:
    print(
        json.dumps(
            {
                "event": "feature_label_progress",
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
