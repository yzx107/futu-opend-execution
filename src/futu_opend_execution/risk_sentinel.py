"""Abnormal-market sentinel for watchlist monitoring.

The sentinel is a detector, not a predictor: it emits events that pause,
alert, or require review before any dry-run strategy signal is consumed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from futu_opend_execution.data.market import MarketState, market_state_to_jsonable
from futu_opend_execution.watchlist import BlackSwanThresholds, WatchSymbolConfig


class RiskSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class RiskCategory(str, Enum):
    PRICE_CRASH = "PRICE_CRASH"
    GAP_DOWN = "GAP_DOWN"
    LIQUIDITY_VANISH = "LIQUIDITY_VANISH"
    SPREAD_WIDEN = "SPREAD_WIDEN"
    ORDERBOOK_IMBALANCE = "ORDERBOOK_IMBALANCE"
    VOL_SPIKE = "VOL_SPIKE"
    DATA_STALE = "DATA_STALE"
    OPEN_D_DISCONNECT = "OPEN_D_DISCONNECT"
    MANUAL_KILL_SWITCH = "MANUAL_KILL_SWITCH"


class RiskAction(str, Enum):
    WAIT = "WAIT"
    ALERT_ONLY = "ALERT_ONLY"
    PAUSE_TRADING = "PAUSE_TRADING"
    REQUIRE_MANUAL_REVIEW = "REQUIRE_MANUAL_REVIEW"
    SUGGEST_SELL_TRADING_BUCKET = "SUGGEST_SELL_TRADING_BUCKET"


@dataclass(frozen=True, slots=True)
class RiskEvent:
    symbol: str
    timestamp: str
    severity: RiskSeverity
    category: RiskCategory
    action: RiskAction
    message: str
    market_snapshot: dict[str, Any]

    def to_jsonable(self, *, mode: str = "live-dry-run", source: str = "risk_sentinel") -> dict[str, Any]:
        return {
            "event": "risk_event",
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "mode": mode,
            "source": source,
            "severity": self.severity.value,
            "category": self.category.value,
            "action": self.action.value,
            "message": self.message,
            "market_snapshot": self.market_snapshot,
            "payload": {
                **asdict(self),
                "severity": self.severity.value,
                "category": self.category.value,
                "action": self.action.value,
            },
        }


class BlackSwanSentinel:
    """Detect abnormal states for a user-selected symbol."""

    def evaluate(
        self,
        *,
        market: MarketState,
        config: WatchSymbolConfig,
        now: datetime | None = None,
    ) -> list[RiskEvent]:
        thresholds = config.black_swan_thresholds
        events: list[RiskEvent] = []
        observed_at = now or datetime.now(tz=market.timestamp.tzinfo)
        age_seconds = _age_seconds(observed_at, market.timestamp)

        if market.stale or age_seconds > thresholds.stale_seconds:
            events.append(
                _event(
                    market,
                    RiskSeverity.WARNING,
                    RiskCategory.DATA_STALE,
                    RiskAction.PAUSE_TRADING,
                    f"market data stale: age_seconds={_str_decimal(age_seconds)} threshold={_str_decimal(thresholds.stale_seconds)}",
                )
            )

        events.extend(self._price_crash_events(market, thresholds))

        if (
            market.previous_close is not None
            and market.previous_close > 0
            and market.open_price is not None
            and market.open_price > 0
        ):
            gap_bps = _drop_bps(market.previous_close, market.open_price)
            if gap_bps >= thresholds.gap_down_bps:
                events.append(
                    _event(
                        market,
                        RiskSeverity.CRITICAL,
                        RiskCategory.GAP_DOWN,
                        RiskAction.REQUIRE_MANUAL_REVIEW,
                        f"gap down { _str_decimal(gap_bps) } bps exceeds { _str_decimal(thresholds.gap_down_bps) } bps",
                    )
                )

        if market.spread_bps > thresholds.spread_bps:
            events.append(
                _event(
                    market,
                    RiskSeverity.WARNING,
                    RiskCategory.SPREAD_WIDEN,
                    RiskAction.PAUSE_TRADING,
                    f"spread { _str_decimal(market.spread_bps) } bps exceeds { _str_decimal(thresholds.spread_bps) } bps",
                )
            )

        min_bid_size = Decimal(thresholds.min_bid_size_lots * config.lot_size)
        if market.best_bid is None or market.bid_size < min_bid_size:
            events.append(
                _event(
                    market,
                    RiskSeverity.WARNING,
                    RiskCategory.LIQUIDITY_VANISH,
                    RiskAction.PAUSE_TRADING,
                    f"best bid missing or bid_size={_str_decimal(market.bid_size)} below { _str_decimal(min_bid_size) }",
                )
            )

        if abs(market.orderbook_imbalance) >= Decimal("0.95"):
            events.append(
                _event(
                    market,
                    RiskSeverity.WARNING,
                    RiskCategory.ORDERBOOK_IMBALANCE,
                    RiskAction.ALERT_ONLY,
                    f"extreme orderbook imbalance { _str_decimal(market.orderbook_imbalance) }",
                )
            )

        return events

    def provider_error(
        self,
        *,
        symbol: str,
        message: str,
        timestamp: datetime | None = None,
    ) -> RiskEvent:
        ts = timestamp or datetime.now().astimezone()
        return RiskEvent(
            symbol=symbol,
            timestamp=ts.isoformat(),
            severity=RiskSeverity.CRITICAL,
            category=RiskCategory.OPEN_D_DISCONNECT,
            action=RiskAction.PAUSE_TRADING,
            message=message,
            market_snapshot={},
        )

    def _price_crash_events(self, market: MarketState, thresholds: BlackSwanThresholds) -> list[RiskEvent]:
        if market.last_price is None or market.last_price <= 0:
            return []
        anchors = {
            "previous_close": market.previous_close,
            "open_price": market.open_price,
            "rolling_vwap": market.rolling_vwap,
            "rolling_high": market.rolling_high,
        }
        drops = {
            name: _drop_bps(anchor, market.last_price)
            for name, anchor in anchors.items()
            if anchor is not None and anchor > 0
        }
        if not drops:
            return []
        worst_name, worst_drop = max(drops.items(), key=lambda item: item[1])
        if worst_drop < thresholds.intraday_drop_bps:
            return []
        return [
            _event(
                market,
                RiskSeverity.CRITICAL,
                RiskCategory.PRICE_CRASH,
                RiskAction.REQUIRE_MANUAL_REVIEW,
                f"drop from {worst_name} is { _str_decimal(worst_drop) } bps, threshold={ _str_decimal(thresholds.intraday_drop_bps) } bps",
            )
        ]


def risk_events_to_jsonl(
    events: list[RiskEvent],
    *,
    monitor_log_path: Path | str,
    critical_log_path: Path | str = "logs/agent/risk_events.jsonl",
    mode: str = "live-dry-run",
) -> None:
    for event in events:
        row = event.to_jsonable(mode=mode)
        _append_jsonl(monitor_log_path, row)
        if event.severity is RiskSeverity.CRITICAL:
            _append_jsonl(critical_log_path, row)


def should_pause_trading(events: list[RiskEvent]) -> bool:
    return any(event.action in {RiskAction.PAUSE_TRADING, RiskAction.REQUIRE_MANUAL_REVIEW} for event in events)


def _event(
    market: MarketState,
    severity: RiskSeverity,
    category: RiskCategory,
    action: RiskAction,
    message: str,
) -> RiskEvent:
    return RiskEvent(
        symbol=market.symbol,
        timestamp=market.timestamp.isoformat(),
        severity=severity,
        category=category,
        action=action,
        message=message,
        market_snapshot=market_state_to_jsonable(market),
    )


def _drop_bps(anchor: Decimal, value: Decimal) -> Decimal:
    if anchor <= 0 or value >= anchor:
        return Decimal("0")
    return (anchor - value) / anchor * Decimal("10000")


def _age_seconds(now: datetime, then: datetime) -> Decimal:
    if now.tzinfo is None and then.tzinfo is not None:
        now = now.replace(tzinfo=then.tzinfo)
    if now.tzinfo is not None and then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    return Decimal(str(max((now - then).total_seconds(), 0.0)))


def _append_jsonl(path: Path | str, row: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
