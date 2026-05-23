"""Quality-gated Hshare top-of-book replay adapter."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution.data.market import MarketEvent, MarketState, build_market_states

DEFAULT_HSHARE_TOP_OF_BOOK_ROOT = Path(
    "/Volumes/Data/港股Tick数据/caveat/orderbook_replay__top_of_book_only"
)
NAMESPACE = "orderbook_replay__top_of_book_only"


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
        for symbol in self.symbols:
            events = [event for event in self.iter_events() if event.symbol == symbol]
            yield from build_market_states(
                events,
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
            rows.extend(_read_parquet_rows(path, limit_rows=self.limit_rows))
        return rows


def _row_events(
    row: dict[str, Any],
    *,
    symbol: str,
    require_quality: bool,
) -> list[MarketEvent]:
    timestamp = _row_time(row)
    output = [
        MarketEvent(
            symbol=symbol,
            timestamp=timestamp,
            event_type="trade",
            price=row.get("TradePrice") or row.get("Price"),
            volume=row.get("TradeVolume") or row.get("Volume") or 0,
        )
    ]

    quality_ok = _quality_ok(row)
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
    return (
        bool(row.get("TopOfBookValidFlag"))
        and _decimal(row.get("ReplayQualityScore")) == Decimal("1.0")
        and not bool(row.get("CrossedWindowFlag"))
        and not bool(row.get("ReplayResidueFlag"))
        and not bool(row.get("ReplayWindowExcludedFlag"))
        and not bool(row.get("SameMillisecondBatchRiskFlag"))
    )


def _namespace_root(root: Path) -> Path:
    if (root / "top_of_book_events").exists() or root.name == NAMESPACE:
        return root
    return root / NAMESPACE


def _read_parquet_rows(path: Path, *, limit_rows: int | None) -> list[dict[str, Any]]:
    try:
        import polars as pl  # type: ignore
    except Exception:
        pl = None
    if pl is not None:
        frame = pl.scan_parquet(str(path))
        if limit_rows is not None:
            frame = frame.limit(limit_rows)
        return frame.collect().to_dicts()

    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Reading Hshare top-of-book parquet requires polars or pandas+pyarrow") from exc
    frame = pd.read_parquet(path)
    if limit_rows is not None:
        frame = frame.head(limit_rows)
    return frame.to_dict("records")


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


def _decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))
