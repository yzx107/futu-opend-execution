#!/usr/bin/env python3
"""Probe Hshare order parquet semantics for order-book reconstruction."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from futu_opend_execution.data.hshare_l2 import DEFAULT_HSHARE_L2_ROOT
from futu_opend_execution.data.hshare_orderbook import (
    default_probe_paths,
    probe_orderbook_semantics,
    symbol_code,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Hshare order-book semantics")
    parser.add_argument("symbol", help="HK symbol, e.g. HK.01879")
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD")
    parser.add_argument("--data-root", default=str(DEFAULT_HSHARE_L2_ROOT))
    parser.add_argument("--report-dir", default="reports/agent")
    parser.add_argument("--limit-rows", type=int)
    args = parser.parse_args()

    order_path, trade_path = default_probe_paths(args.data_root, date=args.date)
    code = symbol_code(args.symbol)
    order_rows = _read_symbol_rows(order_path, code, limit_rows=args.limit_rows)
    trade_rows = _read_symbol_rows(trade_path, code, limit_rows=args.limit_rows)
    result = probe_orderbook_semantics(order_rows=order_rows, trade_rows=trade_rows)
    result.update(
        {
            "symbol": f"HK.{code}",
            "date": args.date,
            "order_path": str(order_path),
            "trade_path": str(trade_path),
            "order_rows": len(order_rows),
            "trade_rows": len(trade_rows),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"hshare_orderbook_probe_{code}_{args.date}"
    json_path = report_dir / f"{stem}.json"
    md_path = report_dir / f"{stem}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(_markdown_report(result), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), **_summary(result)}, ensure_ascii=False, indent=2))
    return 0


def _read_symbol_rows(path: Path, code: str, *, limit_rows: int | None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    suffix = f"/{code}.csv"
    try:
        import polars as pl  # type: ignore
    except Exception:
        pl = None
    if pl is not None:
        frame = pl.scan_parquet(str(path))
        if "source_file" in frame.collect_schema().names():
            frame = frame.filter(pl.col("source_file").str.ends_with(suffix))
        if limit_rows is not None:
            frame = frame.limit(limit_rows)
        return frame.collect().to_dicts()

    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Reading Hshare parquet requires polars or pandas+pyarrow") from exc
    frame = pd.read_parquet(path)
    if "source_file" in frame.columns:
        frame = frame[frame["source_file"].astype(str).str.endswith(suffix)]
    if limit_rows is not None:
        frame = frame.head(limit_rows)
    return frame.to_dict("records")


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    chosen = next(item for item in result["candidate_results"] if item["side_bit"] == result["recommended_side_bit"])
    return {
        "symbol": result["symbol"],
        "date": result["date"],
        "order_rows": result["order_rows"],
        "trade_rows": result["trade_rows"],
        "recommended_side_bit": result["recommended_side_bit"],
        "crossed_book_rate": chosen["crossed_book_rate"],
        "level_match_rate": chosen["level_match_rate"],
        "bid_orderid_side_match_rate": chosen["bid_orderid_side_match_rate"],
        "ask_orderid_side_match_rate": chosen["ask_orderid_side_match_rate"],
    }


def _markdown_report(result: dict[str, Any]) -> str:
    lines = [
        f"# Hshare Order Book Probe {result['symbol']} {result['date']}",
        "",
        f"- order rows: {result['order_rows']}",
        f"- trade rows: {result['trade_rows']}",
        f"- recommended `Ext` side bit: `{result['recommended_side_bit']}`",
        f"- basis: {result['recommendation_basis']}",
        "",
        "| side_bit | crossed_book_rate | level_match_rate | bid_id_side_match | ask_id_side_match | volume_pre_match |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in result["candidate_results"]:
        lines.append(
            "| {side_bit} | {crossed_book_rate} | {level_match_rate} | {bid_orderid_side_match_rate} | "
            "{ask_orderid_side_match_rate} | {volume_pre_match_rate} |".format(**item)
        )
    lines.extend(
        [
            "",
            "Notes:",
            "- This is a semantics probe, not a production trading input.",
            "- Only wire reconstructed book fields into replay after crossed-book and linkage rates are acceptable.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
