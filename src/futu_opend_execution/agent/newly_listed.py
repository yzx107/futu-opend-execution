"""Newly listed HK stock universe and batch cost-reducer optimization."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Iterable

from futu_opend_execution.agent.optimizer import CostReducerGrid, optimize_cost_reducer
from futu_opend_execution.agent.runtime import TradingAgentConfig
from futu_opend_execution.data.hshare_l2 import DEFAULT_HSHARE_L2_ROOT, HshareL2ReplayProvider
from futu_opend_execution.data.hshare_top_of_book import HshareTopOfBookReplayProvider, symbol_code

DEFAULT_INSTRUMENT_PROFILE = Path("/Volumes/Data/港股Tick数据/reference/instrument_profile/latest/instrument_profile.parquet")
DEFAULT_TOP_OF_BOOK_ROOT = Path("/Volumes/Data/港股Tick数据/caveat/orderbook_replay__top_of_book_only")


@dataclass(frozen=True, slots=True)
class NewlyListedCandidate:
    symbol: str
    listing_date: str
    observed_first_date: str | None
    observed_last_date: str | None
    observed_trades_days: int
    observed_orders_days: int
    available_trade_dates: tuple[str, ...]
    instrument_family: str | None
    stock_research_candidate_status: str | None
    source_label: str | None

    def to_jsonable(self) -> dict[str, Any]:
        return {**asdict(self), "available_trade_dates": list(self.available_trade_dates)}


def build_newly_listed_universe(
    *,
    instrument_profile_path: Path | str = DEFAULT_INSTRUMENT_PROFILE,
    data_root: Path | str = DEFAULT_HSHARE_L2_ROOT,
    top_of_book_root: Path | str | None = None,
    listing_year: int = 2026,
    as_of: date | None = None,
    dates: Iterable[str] | None = None,
    min_trade_dates: int = 1,
    stock_research_candidate_only: bool = True,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    candidates = _load_profile_candidates(
        Path(instrument_profile_path),
        listing_year=listing_year,
        as_of=as_of,
        stock_research_candidate_only=stock_research_candidate_only,
    )
    wanted_dates = tuple(dates or ())
    data_path = Path(data_root)
    top_path = Path(top_of_book_root) if top_of_book_root else None
    coverage = None if top_path is not None else _candidate_cleaned_coverage(data_path, dates=wanted_dates)
    rows: list[NewlyListedCandidate] = []
    for item in candidates:
        available_dates = _available_dates(
            symbol=item["instrument_key"],
            listing_date=str(item["listing_date"]),
            data_root=data_path,
            top_of_book_root=top_path,
            dates=wanted_dates,
            coverage=coverage,
        )
        if len(available_dates) < min_trade_dates:
            continue
        rows.append(
            NewlyListedCandidate(
                symbol=f"HK.{item['instrument_key']}",
                listing_date=str(item["listing_date"]),
                observed_first_date=_date_str(item.get("observed_first_date")),
                observed_last_date=_date_str(item.get("observed_last_date")),
                observed_trades_days=int(item.get("observed_trades_days") or 0),
                observed_orders_days=int(item.get("observed_orders_days") or 0),
                available_trade_dates=tuple(available_dates),
                instrument_family=item.get("instrument_family"),
                stock_research_candidate_status=item.get("stock_research_candidate_status"),
                source_label=item.get("source_label"),
            )
        )
        if max_symbols is not None and len(rows) >= max_symbols:
            break
    return {
        "event": "newly_listed_universe",
        "listing_year": listing_year,
        "as_of": as_of.isoformat() if as_of else None,
        "source": str(instrument_profile_path),
        "data_root": str(top_of_book_root or data_root),
        "candidate_count": len(rows),
        "stock_research_candidate_only": stock_research_candidate_only,
        "min_trade_dates": min_trade_dates,
        "candidates": [row.to_jsonable() for row in rows],
        "limitations": [
            "instrument_profile is a sidecar universe source, not a verified security master",
            "stock_research_candidate is a conservative research lane, not pure common-equity proof",
        ],
    }


def optimize_newly_listed(
    *,
    instrument_profile_path: Path | str = DEFAULT_INSTRUMENT_PROFILE,
    data_root: Path | str = DEFAULT_HSHARE_L2_ROOT,
    top_of_book_root: Path | str | None = None,
    listing_year: int = 2026,
    dates: Iterable[str] | None = None,
    min_trade_dates: int = 1,
    max_symbols: int | None = None,
    max_dates_per_symbol: int | None = None,
    lot_size: int = 1,
    current_qty: int = 2,
    grid: CostReducerGrid = CostReducerGrid(),
    top_n: int = 20,
) -> dict[str, Any]:
    universe = build_newly_listed_universe(
        instrument_profile_path=instrument_profile_path,
        data_root=data_root,
        top_of_book_root=top_of_book_root,
        listing_year=listing_year,
        dates=dates,
        min_trade_dates=min_trade_dates,
        max_symbols=max_symbols,
    )
    per_case: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for candidate in universe["candidates"]:
        symbol = str(candidate["symbol"])
        run_dates = list(candidate["available_trade_dates"])
        if max_dates_per_symbol is not None:
            run_dates = run_dates[-max_dates_per_symbol:]
        for trade_date in run_dates:
            try:
                states = _market_states(symbol, trade_date, data_root, top_of_book_root)
                if not states:
                    continue
                cost_price = _initial_cost_price(states)
                if cost_price is None or cost_price <= 0:
                    failures.append({"symbol": symbol, "date": trade_date, "error": "initial cost price unavailable"})
                    continue
                config = TradingAgentConfig(symbol, current_qty=current_qty, cost_price=cost_price, lot_size=lot_size)
                summary = optimize_cost_reducer(config=config, market_states=states, grid=grid, top_n=10_000)
                for row in summary["results"]:
                    per_case.append(
                        {
                            "symbol": symbol,
                            "date": trade_date,
                            "listing_date": candidate["listing_date"],
                            **row,
                        }
                    )
            except Exception as exc:  # pragma: no cover - operational guardrail
                failures.append({"symbol": symbol, "date": trade_date, "error": repr(exc)})
    ranking = _aggregate_rankings(per_case)[:top_n]
    return {
        "event": "newly_listed_optimizer_summary",
        "listing_year": listing_year,
        "universe": universe,
        "evaluated_case_count": len({(row["symbol"], row["date"]) for row in per_case}),
        "result_row_count": len(per_case),
        "failure_count": len(failures),
        "failures": failures,
        "assumptions": {
            "lot_size": lot_size,
            "current_qty": current_qty,
            "position_model": "normalized existing position; not account holdings",
        },
        "ranking": ranking,
        "per_case_results": per_case,
    }


def write_newly_listed_reports(summary: dict[str, Any], *, json_path: Path | str, markdown_path: Path | str | None = None) -> None:
    target = Path(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if markdown_path is not None:
        md = Path(markdown_path)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(_markdown(summary), encoding="utf-8")


def _load_profile_candidates(
    path: Path,
    *,
    listing_year: int,
    as_of: date | None,
    stock_research_candidate_only: bool,
) -> list[dict[str, Any]]:
    try:
        import polars as pl  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("newly-listed universe requires polars to read instrument_profile parquet") from exc
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pl.scan_parquet(str(path)).filter(
        (pl.col("listing_date") >= date(listing_year, 1, 1))
        & (pl.col("listing_date") <= (as_of or date(listing_year, 12, 31)))
    )
    if stock_research_candidate_only:
        frame = frame.filter(pl.col("stock_research_candidate") == True)
    return (
        frame.select(
            [
                "instrument_key",
                "listing_date",
                "observed_first_date",
                "observed_last_date",
                "observed_trades_days",
                "observed_orders_days",
                "instrument_family",
                "stock_research_candidate_status",
                "source_label",
            ]
        )
        .sort(["listing_date", "instrument_key"])
        .collect()
        .to_dicts()
    )


def _available_dates(
    *,
    symbol: str,
    listing_date: str,
    data_root: Path,
    top_of_book_root: Path | None,
    dates: tuple[str, ...],
    coverage: dict[str, tuple[str, ...]] | None,
) -> list[str]:
    if top_of_book_root is not None:
        return [value for value in _top_of_book_dates(symbol=symbol, top_of_book_root=top_of_book_root, dates=dates) if value >= listing_date]
    if coverage is not None:
        return [value for value in coverage.get(symbol_code(symbol), ()) if value >= listing_date]
    date_values = dates or tuple(_candidate_cleaned_dates(data_root))
    output = []
    for value in date_values:
        if value < listing_date:
            continue
        if _candidate_cleaned_has_symbol(data_root, date=value, symbol=symbol):
            output.append(value)
    return output


def _candidate_cleaned_dates(data_root: Path) -> list[str]:
    return sorted(path.name.removeprefix("date=") for path in (data_root / "trades").glob("date=*") if path.is_dir())


def _candidate_cleaned_coverage(data_root: Path, *, dates: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    date_values = dates or tuple(_candidate_cleaned_dates(data_root))
    coverage: dict[str, list[str]] = defaultdict(list)
    for value in date_values:
        path = data_root / "trades" / f"date={value}" / f"{value.replace('-', '')}_trades.parquet"
        if not path.exists():
            continue
        for code in _candidate_cleaned_symbols(path):
            coverage[code].append(value)
    return {code: tuple(values) for code, values in coverage.items()}


def _candidate_cleaned_symbols(path: Path) -> list[str]:
    try:
        import polars as pl  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("candidate_cleaned symbol discovery requires polars") from exc
    frame = pl.scan_parquet(str(path))
    if "source_file" not in frame.collect_schema().names():
        return []
    rows = frame.select(pl.col("source_file").drop_nulls().unique()).collect()
    return sorted({Path(str(value)).stem.zfill(5) for value in rows["source_file"]})


def _candidate_cleaned_has_symbol(data_root: Path, *, date: str, symbol: str) -> bool:
    path = data_root / "trades" / f"date={date}" / f"{date.replace('-', '')}_trades.parquet"
    if not path.exists():
        return False
    try:
        import polars as pl  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("candidate_cleaned symbol discovery requires polars") from exc
    suffix = f"/{symbol_code(symbol)}.csv"
    return bool(
        pl.scan_parquet(str(path))
        .filter(pl.col("source_file").str.ends_with(suffix))
        .select(pl.len())
        .limit(1)
        .collect()
        .item()
    )


def _top_of_book_dates(*, symbol: str, top_of_book_root: Path, dates: tuple[str, ...]) -> list[str]:
    root = top_of_book_root
    if not (root / "top_of_book_events").exists():
        root = root / "orderbook_replay__top_of_book_only"
    code = symbol_code(symbol)
    output = []
    for year_dir in sorted((root / "top_of_book_events").glob("year=*")):
        for date_dir in sorted(year_dir.glob("date=*")):
            value = date_dir.name.removeprefix("date=")
            if dates and value not in dates:
                continue
            if (date_dir / f"symbol={code}").exists():
                output.append(value)
    return output


def _market_states(symbol: str, trade_date: str, data_root: Path | str, top_of_book_root: Path | str | None):
    if top_of_book_root:
        provider = HshareTopOfBookReplayProvider(data_root=top_of_book_root, dates=[trade_date], symbols=[symbol])
    else:
        provider = HshareL2ReplayProvider(data_root=data_root, dates=[trade_date], symbols=[symbol])
    return list(provider.iter_market_states())


def _initial_cost_price(states) -> Decimal | None:
    for state in states:
        value = state.opening_vwap or state.rolling_vwap or state.last_price
        if value is not None and value > 0:
            return value
    return None


def _aggregate_rankings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = json.dumps(row["params"], sort_keys=True, ensure_ascii=False)
        bucket = buckets.setdefault(
            key,
            {
                "params": row["params"],
                "cases": 0,
                "score_sum": Decimal("0"),
                "net_pnl_after_cost_sum": Decimal("0"),
                "realized_net_pnl_sum": Decimal("0"),
                "cost_basis_reduction_sum": Decimal("0"),
                "round_trips_completed": 0,
                "sell_count": 0,
                "rebuy_count": 0,
                "open_quantity_sum": 0,
                "open_quantity_penalty_sum": Decimal("0"),
                "risk_block_count": 0,
                "quality_block_count": 0,
            },
        )
        bucket["cases"] += 1
        bucket["score_sum"] += _decimal(row["score"])
        bucket["net_pnl_after_cost_sum"] += _decimal(row.get("net_pnl_after_cost", row["realized_net_pnl"]))
        bucket["realized_net_pnl_sum"] += _decimal(row["realized_net_pnl"])
        bucket["cost_basis_reduction_sum"] += _decimal(row["cost_basis_reduction"])
        bucket["round_trips_completed"] += int(row["round_trips_completed"])
        bucket["sell_count"] += int(row["sell_count"])
        bucket["rebuy_count"] += int(row["rebuy_count"])
        bucket["open_quantity_sum"] += int(row["open_quantity"])
        bucket["open_quantity_penalty_sum"] += _decimal(row.get("open_quantity_penalty", Decimal("0")))
        bucket["risk_block_count"] += int(row["risk_block_count"])
        bucket["quality_block_count"] += int(row["quality_block_count"])
    ranked = list(buckets.values())
    ranked.sort(
        key=lambda item: (
            item["score_sum"],
            item["realized_net_pnl_sum"],
            item["round_trips_completed"],
            -item["open_quantity_sum"],
        ),
        reverse=True,
    )
    return [_jsonable_bucket(item) for item in ranked]


def _jsonable_bucket(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in item.items()
    }


def _markdown(summary: dict[str, Any]) -> str:
    if summary.get("event") == "newly_listed_universe":
        lines = [
            "# Newly Listed HK Universe",
            "",
            f"- listing_year: {summary['listing_year']}",
            f"- candidate_count: {summary['candidate_count']}",
            f"- source: `{summary['source']}`",
            f"- data_root: `{summary['data_root']}`",
            "",
            "| symbol | listing_date | available_dates | observed_trades_days | source |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        for row in summary["candidates"]:
            lines.append(
                f"| {row['symbol']} | {row['listing_date']} | {len(row['available_trade_dates'])} | {row['observed_trades_days']} | {row.get('source_label')} |"
            )
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in summary["limitations"])
        return "\n".join(lines) + "\n"

    lines = [
        "# Newly Listed Cost Reducer Optimization",
        "",
        f"- listing_year: {summary['listing_year']}",
        f"- universe_count: {summary['universe']['candidate_count']}",
        f"- evaluated_case_count: {summary['evaluated_case_count']}",
        f"- failure_count: {summary['failure_count']}",
        f"- assumptions: `{summary['assumptions']}`",
        "",
        "## Ranking",
        "",
        "| rank | score_sum | cases | net_pnl_after_cost | cost_basis_reduction | round_trips | open_qty | open_qty_penalty | quality_blocks | params |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for index, row in enumerate(summary["ranking"], start=1):
        lines.append(
            "| {rank} | {score_sum} | {cases} | {net_pnl_after_cost_sum} | {cost_basis_reduction_sum} | {round_trips_completed} | {open_quantity_sum} | {open_quantity_penalty_sum} | {quality_block_count} | `{params}` |".format(
                rank=index,
                **row,
            )
        )
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- This is paper/replay research only, not a real-trading recommendation.",
            "- The universe uses instrument_profile sidecar listing_date and stock_research_candidate flags.",
            "- Default lot assumptions are normalized when board lot is unavailable.",
        ]
    )
    return "\n".join(lines) + "\n"


def _date_str(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
