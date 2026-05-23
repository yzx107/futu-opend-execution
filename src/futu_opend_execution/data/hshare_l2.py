"""Hshare Lab v2 candidate_cleaned L2 replay adapter."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution.data.market import MarketEvent, MarketState, build_market_states

DEFAULT_HSHARE_L2_ROOT = Path("/Volumes/Data/港股Tick数据/candidate_cleaned")


class HshareL2ReplayProvider:
    """Read Hshare candidate_cleaned trades/orders parquet and emit market states."""

    def __init__(
        self,
        *,
        data_root: Path | str = DEFAULT_HSHARE_L2_ROOT,
        dates: Iterable[str],
        symbols: Iterable[str],
        interval_seconds: int = 1,
        limit_rows: int | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.dates = tuple(dates)
        self.symbols = tuple(_normalize_symbol(symbol) for symbol in symbols)
        self.interval_seconds = max(int(interval_seconds), 1)
        self.limit_rows = limit_rows

    def iter_events(self) -> Iterable[MarketEvent]:
        wanted = {symbol.split(".", 1)[1] for symbol in self.symbols}
        for date in self.dates:
            for row in self._read_kind(date, "trades", symbols=wanted):
                symbol = _symbol_from_row(row)
                yield MarketEvent(
                    symbol=symbol,
                    timestamp=_row_time(row),
                    event_type="trade",
                    price=row.get("Price"),
                    volume=row.get("Volume") or 0,
                    turnover=_turnover(row),
                    side=_trade_side(row.get("Dir")),
                )
            for row in self._read_kind(date, "orders", symbols=wanted):
                symbol = _symbol_from_row(row)
                yield MarketEvent(
                    symbol=symbol,
                    timestamp=_row_time(row),
                    event_type="order",
                    price=row.get("Price"),
                    volume=row.get("Volume") or 0,
                    side=_order_side(row),
                )

    def iter_market_states(self) -> Iterable[MarketState]:
        for symbol in self.symbols:
            events = [event for event in self.iter_events() if event.symbol == symbol]
            yield from build_market_states(
                events,
                interval_seconds=self.interval_seconds,
                source="hshare_l2",
            )

    @classmethod
    def from_events(
        cls,
        events: Iterable[MarketEvent],
        *,
        interval_seconds: int = 1,
    ) -> "InMemoryReplayProvider":
        return InMemoryReplayProvider(events, interval_seconds=interval_seconds)

    def _read_kind(self, date: str, kind: str, *, symbols: set[str]) -> list[dict[str, Any]]:
        files = sorted((self.data_root / kind / f"date={date}").glob("*.parquet"))
        rows: list[dict[str, Any]] = []
        for path in files:
            rows.extend(_read_parquet_rows(path, limit_rows=self.limit_rows, symbols=symbols))
        return rows


class InMemoryReplayProvider:
    def __init__(self, events: Iterable[MarketEvent], *, interval_seconds: int = 1) -> None:
        self.events = tuple(events)
        self.interval_seconds = interval_seconds

    def iter_events(self) -> Iterable[MarketEvent]:
        return iter(self.events)

    def iter_market_states(self) -> Iterable[MarketState]:
        yield from build_market_states(self.events, interval_seconds=self.interval_seconds, source="fixture")


def _read_parquet_rows(path: Path, *, limit_rows: int | None, symbols: set[str]) -> list[dict[str, Any]]:
    try:
        import polars as pl  # type: ignore
    except Exception:
        pl = None
    if pl is not None:
        frame = pl.scan_parquet(str(path))
        if symbols and "source_file" in frame.collect_schema().names():
            suffixes = [f"/{symbol}.csv" for symbol in symbols]
            frame = frame.filter(pl.any_horizontal([pl.col("source_file").str.ends_with(suffix) for suffix in suffixes]))
        if limit_rows is not None:
            frame = frame.limit(limit_rows)
        return frame.collect().to_dicts()

    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only on hosts without parquet dependencies
        raise RuntimeError("Reading Hshare parquet requires polars or pandas+pyarrow") from exc
    frame = pd.read_parquet(path)
    if symbols and "source_file" in frame.columns:
        suffixes = tuple(f"/{symbol}.csv" for symbol in symbols)
        frame = frame[frame["source_file"].astype(str).str.endswith(suffixes)]
    if limit_rows is not None:
        frame = frame.head(limit_rows)
    return frame.to_dict("records")


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        normalized = f"HK.{normalized}"
    return normalized


def _symbol_from_row(row: dict[str, Any]) -> str:
    source_file = str(row.get("source_file") or "")
    code = Path(source_file).stem if source_file else str(row.get("symbol") or "")
    code = code.strip().upper()
    if "." not in code:
        code = f"HK.{code.zfill(5)}"
    return code


def _row_time(row: dict[str, Any]) -> datetime:
    value = row.get("SendTime") or row.get("timestamp") or row.get("Time")
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _turnover(row: dict[str, Any]) -> Decimal:
    if row.get("Turnover") not in {None, ""}:
        return Decimal(str(row["Turnover"]))
    return Decimal(str(row.get("Price") or 0)) * Decimal(str(row.get("Volume") or 0))


def _trade_side(value: Any) -> str | None:
    text = str(value).strip().upper()
    if text in {"B", "BUY", "1"}:
        return "BUY"
    if text in {"S", "SELL", "2"}:
        return "SELL"
    return None


def _order_side(row: dict[str, Any]) -> str | None:
    for key in ("side", "Side", "BS", "Direction"):
        if row.get(key) not in {None, ""}:
            return _trade_side(row[key])
    return None
