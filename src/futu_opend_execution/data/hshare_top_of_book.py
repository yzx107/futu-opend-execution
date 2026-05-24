"""Quality-gated Hshare top-of-book replay adapter."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution.data.market import MarketEvent, MarketState, build_market_states

DEFAULT_HSHARE_TOP_OF_BOOK_ROOT = Path(
    "/Volumes/Data/港股Tick数据/caveat/orderbook_replay__top_of_book_with_size_caveat"
)
NAMESPACE = "orderbook_replay__top_of_book_only"
SIZE_CAVEAT_NAMESPACE = "orderbook_replay__top_of_book_with_size_caveat"
SUPPORTED_NAMESPACES = (SIZE_CAVEAT_NAMESPACE, NAMESPACE)


class HshareTopOfBookReplayProvider:
    """Read Hshare Lab v2 top-of-book-only output with explicit quality gates."""

    def __init__(
        self,
        *,
        data_root: Path | str = DEFAULT_HSHARE_TOP_OF_BOOK_ROOT,
        dates: Iterable[str],
        symbols: Iterable[str],
        interval_seconds: int = 1,
        limit_rows: int | None = None,
        require_quality: bool = True,
    ) -> None:
        self.data_root = _namespace_root(Path(data_root))
        self.dates = tuple(dates)
        self.symbols = tuple(_normalize_symbol(symbol) for symbol in symbols)
        self.interval_seconds = max(int(interval_seconds), 1)
        self.limit_rows = limit_rows
        self.require_quality = require_quality

    def iter_events(self) -> Iterable[MarketEvent]:
        for date in self.dates:
            for symbol in self.symbols:
                for row in self._read_symbol_rows(date, symbol):
                    yield from _row_events(row, symbol=symbol, require_quality=self.require_quality)

    def iter_market_states(self) -> Iterable[MarketState]:
        events_by_symbol: dict[str, list[MarketEvent]] = {symbol: [] for symbol in self.symbols}
        for event in self.iter_events():
            events_by_symbol.setdefault(event.symbol, []).append(event)
        for symbol in self.symbols:
            yield from build_market_states(
                events_by_symbol.get(symbol, []),
                interval_seconds=self.interval_seconds,
                source="hshare_top_of_book",
            )

    def _read_symbol_rows(self, date: str, symbol: str) -> list[dict[str, Any]]:
        code = symbol_code(symbol)
        partition = (
            self.data_root
            / "top_of_book_events"
            / f"year={date[:4]}"
            / f"date={date}"
            / f"symbol={code}"
        )
        rows: list[dict[str, Any]] = []
        for path in sorted(partition.glob("*.parquet")):
            rows.extend(_read_parquet_rows(path, limit_rows=self.limit_rows, require_quality=self.require_quality))
        return rows


def _row_events(
    row: dict[str, Any],
    *,
    symbol: str,
    require_quality: bool,
) -> list[MarketEvent]:
    timestamp = _row_time(row)
    quality_ok = _quality_ok(row)
    if require_quality and not quality_ok and "StrategyHandoffEligibleFlag" in row:
        return []

    output = [
        MarketEvent(
            symbol=symbol,
            timestamp=timestamp,
            event_type="trade",
            price=row.get("TradePrice") or row.get("Price"),
            volume=row.get("TradeVolume") or row.get("Volume") or 0,
        )
    ]

    if require_quality and not quality_ok:
        output.append(
            MarketEvent(
                symbol=symbol,
                timestamp=timestamp,
                event_type="book_quality",
                book_quality="BLOCKED",
                book_quality_score=row.get("ReplayQualityScore") or 0,
                book_crossed=bool(row.get("CrossedWindowFlag")),
                book_residue=bool(row.get("ReplayResidueFlag")),
                book_window_excluded=bool(row.get("ReplayWindowExcludedFlag")),
                same_millisecond_batch_risk=bool(row.get("SameMillisecondBatchRiskFlag")),
                book_depth_limited=True,
            )
        )
        return output

    bid_size = _first_present(row, ("BidSizeReplay", "BestBidSizeReplay", "bid_size", "BidSize"))
    ask_size = _first_present(row, ("AskSizeReplay", "BestAskSizeReplay", "ask_size", "AskSize"))
    depth_limited = bid_size is None or ask_size is None
    output.append(
        MarketEvent(
            symbol=symbol,
            timestamp=timestamp,
            event_type="book",
            bid_price=row.get("BestBidReplay") or row.get("best_bid"),
            bid_size=bid_size or 0,
            ask_price=row.get("BestAskReplay") or row.get("best_ask"),
            ask_size=ask_size or 0,
            book_quality="OK_TOP_OF_BOOK_ONLY" if depth_limited else "OK",
            book_quality_score=row.get("ReplayQualityScore") or 1,
            book_crossed=bool(row.get("CrossedWindowFlag")),
            book_residue=bool(row.get("ReplayResidueFlag")),
            book_window_excluded=bool(row.get("ReplayWindowExcludedFlag")),
            same_millisecond_batch_risk=bool(row.get("SameMillisecondBatchRiskFlag")),
            book_depth_limited=depth_limited,
        )
    )
    return output


def _quality_ok(row: dict[str, Any]) -> bool:
    base_ok = (
        bool(row.get("TopOfBookValidFlag"))
        and _decimal(row.get("ReplayQualityScore")) == Decimal("1.0")
        and not bool(row.get("CrossedWindowFlag"))
        and not bool(row.get("ReplayResidueFlag"))
        and not bool(row.get("ReplayWindowExcludedFlag"))
        and not bool(row.get("SameMillisecondBatchRiskFlag"))
    )
    if not base_ok:
        return False
    if "StrategyHandoffEligibleFlag" not in row:
        return True
    bid = _decimal(row.get("BestBidReplay") or row.get("best_bid"))
    ask = _decimal(row.get("BestAskReplay") or row.get("best_ask"))
    bid_size = _decimal(_first_present(row, ("BidSizeReplay", "BestBidSizeReplay", "bid_size", "BidSize")))
    ask_size = _decimal(_first_present(row, ("AskSizeReplay", "BestAskSizeReplay", "ask_size", "AskSize")))
    return (
        bool(row.get("StrategyHandoffEligibleFlag"))
        and bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and ask >= bid
        and bid_size is not None
        and ask_size is not None
        and bid_size > 0
        and ask_size > 0
    )


def _namespace_root(root: Path) -> Path:
    if (root / "top_of_book_events").exists() or root.name in SUPPORTED_NAMESPACES:
        return root
    for namespace in SUPPORTED_NAMESPACES:
        candidate = root / namespace
        if (candidate / "top_of_book_events").exists():
            return candidate
    return root / SIZE_CAVEAT_NAMESPACE


def resolve_top_of_book_root(root: Path | str) -> Path:
    return _namespace_root(Path(root))


def _read_parquet_rows(path: Path, *, limit_rows: int | None, require_quality: bool) -> list[dict[str, Any]]:
    try:
        import polars as pl  # type: ignore
    except Exception:
        pl = None
    if pl is not None:
        frame = pl.scan_parquet(str(path))
        schema_names = set(frame.collect_schema().names())
        if require_quality and "StrategyHandoffEligibleFlag" in schema_names:
            frame = _filter_polars_strategy_handoff_eligible(frame, schema_names=schema_names, pl=pl)
        if limit_rows is not None:
            frame = frame.limit(limit_rows)
        return frame.collect().to_dicts()

    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Reading Hshare top-of-book parquet requires polars or pandas+pyarrow") from exc
    frame = pd.read_parquet(path)
    if require_quality and "StrategyHandoffEligibleFlag" in frame.columns:
        frame = _filter_pandas_strategy_handoff_eligible(frame)
    if limit_rows is not None:
        frame = frame.head(limit_rows)
    return frame.to_dict("records")


def _filter_polars_strategy_handoff_eligible(frame: Any, *, schema_names: set[str], pl: Any) -> Any:
    bid_size_col = _first_schema_name(schema_names, ("BidSizeReplay", "BestBidSizeReplay", "bid_size", "BidSize"))
    ask_size_col = _first_schema_name(schema_names, ("AskSizeReplay", "BestAskSizeReplay", "ask_size", "AskSize"))
    required = {
        "StrategyHandoffEligibleFlag",
        "TopOfBookValidFlag",
        "ReplayQualityScore",
        "CrossedWindowFlag",
        "ReplayResidueFlag",
        "ReplayWindowExcludedFlag",
        "SameMillisecondBatchRiskFlag",
        "BestBidReplay",
        "BestAskReplay",
    }
    if bid_size_col is None or ask_size_col is None or not required.issubset(schema_names):
        return frame.limit(0)
    bid = pl.col("BestBidReplay").cast(pl.Float64, strict=False)
    ask = pl.col("BestAskReplay").cast(pl.Float64, strict=False)
    bid_size = pl.col(bid_size_col).cast(pl.Float64, strict=False)
    ask_size = pl.col(ask_size_col).cast(pl.Float64, strict=False)
    return frame.filter(
        _polars_bool_col(pl, "StrategyHandoffEligibleFlag")
        & _polars_bool_col(pl, "TopOfBookValidFlag")
        & (pl.col("ReplayQualityScore").cast(pl.Float64, strict=False) == 1.0)
        & ~_polars_bool_col(pl, "CrossedWindowFlag")
        & ~_polars_bool_col(pl, "ReplayResidueFlag")
        & ~_polars_bool_col(pl, "ReplayWindowExcludedFlag")
        & ~_polars_bool_col(pl, "SameMillisecondBatchRiskFlag")
        & (bid > 0)
        & (ask > 0)
        & (ask >= bid)
        & (bid_size > 0)
        & (ask_size > 0)
    )


def _filter_pandas_strategy_handoff_eligible(frame: Any) -> Any:
    bid_size_col = _first_schema_name(set(frame.columns), ("BidSizeReplay", "BestBidSizeReplay", "bid_size", "BidSize"))
    ask_size_col = _first_schema_name(set(frame.columns), ("AskSizeReplay", "BestAskSizeReplay", "ask_size", "AskSize"))
    required = {
        "StrategyHandoffEligibleFlag",
        "TopOfBookValidFlag",
        "ReplayQualityScore",
        "CrossedWindowFlag",
        "ReplayResidueFlag",
        "ReplayWindowExcludedFlag",
        "SameMillisecondBatchRiskFlag",
        "BestBidReplay",
        "BestAskReplay",
    }
    if bid_size_col is None or ask_size_col is None or not required.issubset(set(frame.columns)):
        return frame.iloc[0:0]
    import pandas as pd  # type: ignore

    bid = pd.to_numeric(frame["BestBidReplay"], errors="coerce")
    ask = pd.to_numeric(frame["BestAskReplay"], errors="coerce")
    bid_size = pd.to_numeric(frame[bid_size_col], errors="coerce")
    ask_size = pd.to_numeric(frame[ask_size_col], errors="coerce")
    mask = (
        frame["StrategyHandoffEligibleFlag"].fillna(False).eq(True)
        & frame["TopOfBookValidFlag"].fillna(False).eq(True)
        & (pd.to_numeric(frame["ReplayQualityScore"], errors="coerce") == 1.0)
        & frame["CrossedWindowFlag"].fillna(False).eq(False)
        & frame["ReplayResidueFlag"].fillna(False).eq(False)
        & frame["ReplayWindowExcludedFlag"].fillna(False).eq(False)
        & frame["SameMillisecondBatchRiskFlag"].fillna(False).eq(False)
        & (bid > 0)
        & (ask > 0)
        & (ask >= bid)
        & (bid_size > 0)
        & (ask_size > 0)
    )
    return frame[mask]


def _polars_bool_col(pl: Any, name: str) -> Any:
    return pl.col(name).cast(pl.Boolean, strict=False).fill_null(False)


def _row_time(row: dict[str, Any]) -> datetime:
    value = row.get("SendTime") or row.get("timestamp") or row.get("Time")
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        normalized = f"HK.{normalized.zfill(5)}"
    return normalized


def symbol_code(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        normalized = normalized.split(".", 1)[1]
    return normalized.zfill(5)


def _first_present(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in {None, ""}:
            return value
    return None


def _first_schema_name(names: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in names:
            return candidate
    return None


def _decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))
