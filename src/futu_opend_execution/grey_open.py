"""Grey-market open trigger runner.

This module keeps the open trigger path deliberately small: consume already
subscribed quote/order-book state, evaluate hard risk gates, then either log a
dry-run intent or submit one real limit order through OpenD.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from futu_opend_execution._compat import StrEnum
from pathlib import Path
from threading import Lock
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Any, Iterable, TextIO

from futu_opend_execution._compat import UTC
from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.execution.broker import (
    BrokerConfigurationError,
    BrokerResponseError,
)
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.risk import ExecutionValidationError, validate_runtime_config
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerEngine,
    CostReducerRules,
    CostReducerState,
    apply_dry_run_fill,
)
from futu_opend_execution.services.real_order import (
    GreyMarketRealOrderIntent,
    GreyOrderRole,
    GreyOrderSide,
    GreyOrderSource,
)
from futu_opend_execution.signals.intraday_adaptive import IntradayAdaptiveTracker
from futu_opend_execution.strategy_config import ExecutionMode


FUTU_ORDER_LIMIT_WINDOW_SECONDS = 30.0
FUTU_DOCUMENTED_MAX_ORDERS_PER_WINDOW = 15
SAFE_MAX_ORDERS_PER_WINDOW = 14
SAFE_MIN_ORDER_INTERVAL_SECONDS = 0.05


def _to_decimal(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        return normalized
    return f"HK.{normalized}"


def _strip_market_prefix(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        return normalized
    _, code = normalized.split(".", 1)
    return code


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class TriggerAction(StrEnum):
    WAIT = "WAIT"
    BLOCK = "BLOCK"
    ORDER = "ORDER"


@dataclass(frozen=True, slots=True)
class GreyMarketSignal:
    """Quote/order-book state used by the open trigger."""

    symbol: str
    dark_status: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    orderbook_timestamp: str | None = None
    observed_at: datetime | None = None
    raw_quote: dict[str, Any] | None = None
    raw_order_book: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "dark_status",
            str(self.dark_status or "").strip().upper() or "UNKNOWN",
        )
        object.__setattr__(self, "best_bid", _to_decimal(self.best_bid))
        object.__setattr__(self, "best_ask", _to_decimal(self.best_ask))
        if self.observed_at is None:
            object.__setattr__(self, "observed_at", _utc_now())
        if self.raw_quote is not None:
            object.__setattr__(self, "raw_quote", dict(self.raw_quote))
        if self.raw_order_book is not None:
            object.__setattr__(self, "raw_order_book", dict(self.raw_order_book))


@dataclass(frozen=True, slots=True)
class GreyMarketOrderIntent:
    """A validated order intent emitted by the trigger."""

    symbol: str
    quantity: int
    limit_price: Decimal
    notional: Decimal
    attempt_number: int
    remark: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(self, "limit_price", _to_decimal(self.limit_price))
        object.__setattr__(self, "notional", _to_decimal(self.notional))


@dataclass(frozen=True, slots=True)
class GreyMarketTriggerDecision:
    action: TriggerAction
    reason: str
    intent: GreyMarketOrderIntent | None = None


@dataclass(frozen=True, slots=True)
class GreyMarketTriggerRules:
    """Hard guards for grey-market open automation."""

    symbol: str
    quantity: int
    max_price: Decimal
    max_qty: int
    max_notional: Decimal
    max_order_attempts: int = 3
    cool_down_ms: int = 300
    opening_burst_seconds: float = 0.0
    opening_burst_cool_down_ms: int = 50
    kill_switch_file: Path | None = None
    remark: str = "grey_open_v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(self, "max_price", _to_decimal(self.max_price))
        object.__setattr__(self, "max_notional", _to_decimal(self.max_notional))
        if self.kill_switch_file is not None:
            object.__setattr__(self, "kill_switch_file", Path(self.kill_switch_file))
        self.validate()

    def validate(self) -> None:
        if not self.symbol:
            raise ExecutionValidationError("symbol must not be empty.")
        if self.quantity <= 0:
            raise ExecutionValidationError("quantity must be positive.")
        if self.max_qty <= 0:
            raise ExecutionValidationError("max_qty must be positive.")
        if self.quantity > self.max_qty:
            raise ExecutionValidationError("quantity must not exceed max_qty.")
        if self.max_price <= 0:
            raise ExecutionValidationError("max_price must be positive.")
        if self.max_notional <= 0:
            raise ExecutionValidationError("max_notional must be positive.")
        if self.max_price * self.quantity > self.max_notional:
            raise ExecutionValidationError(
                "max_price * quantity must not exceed max_notional."
            )
        if self.max_order_attempts <= 0:
            raise ExecutionValidationError("max_order_attempts must be positive.")
        if self.max_order_attempts > SAFE_MAX_ORDERS_PER_WINDOW:
            raise ExecutionValidationError(
                "max_order_attempts must stay below the documented OpenD order "
                f"limit of {FUTU_DOCUMENTED_MAX_ORDERS_PER_WINDOW} per 30 seconds."
            )
        if self.cool_down_ms < 0:
            raise ExecutionValidationError("cool_down_ms must be zero or greater.")
        if self.opening_burst_seconds < 0:
            raise ExecutionValidationError(
                "opening_burst_seconds must be zero or greater."
            )
        if self.opening_burst_cool_down_ms < 0:
            raise ExecutionValidationError(
                "opening_burst_cool_down_ms must be zero or greater."
            )


class GreyMarketOpenTrigger:
    """Stateful risk gate for open-triggered orders."""

    def __init__(self, rules: GreyMarketTriggerRules) -> None:
        self._rules = rules
        self._attempts = 0
        self._order_times: deque[float] = deque()
        self._first_trading_seen_at: float | None = None

    @property
    def attempts(self) -> int:
        return self._attempts

    def evaluate(
        self,
        signal: GreyMarketSignal,
        *,
        now_monotonic: float,
    ) -> GreyMarketTriggerDecision:
        if signal.symbol != self._rules.symbol:
            return GreyMarketTriggerDecision(
                action=TriggerAction.WAIT,
                reason=f"symbol mismatch: expected {self._rules.symbol}, got {signal.symbol}",
            )

        if signal.dark_status == "TRADING" and self._first_trading_seen_at is None:
            self._first_trading_seen_at = now_monotonic

        if self._rules.kill_switch_file and self._rules.kill_switch_file.exists():
            return GreyMarketTriggerDecision(
                action=TriggerAction.BLOCK,
                reason=f"kill switch exists: {self._rules.kill_switch_file}",
            )

        if self._attempts >= self._rules.max_order_attempts:
            return GreyMarketTriggerDecision(
                action=TriggerAction.BLOCK,
                reason="max_order_attempts reached",
            )

        self._prune_order_times(now_monotonic)
        if len(self._order_times) >= SAFE_MAX_ORDERS_PER_WINDOW:
            return GreyMarketTriggerDecision(
                action=TriggerAction.BLOCK,
                reason="safe 30-second OpenD order window exhausted",
            )

        if self._order_times:
            elapsed = now_monotonic - self._order_times[-1]
            if elapsed < self._minimum_interval_seconds(now_monotonic):
                return GreyMarketTriggerDecision(
                    action=TriggerAction.WAIT,
                    reason="cool_down_ms/minimum order interval not elapsed",
                )

        if signal.dark_status != "TRADING":
            return GreyMarketTriggerDecision(
                action=TriggerAction.WAIT,
                reason=f"dark_status is {signal.dark_status}",
            )

        if signal.best_ask is None or signal.best_ask <= 0:
            return GreyMarketTriggerDecision(
                action=TriggerAction.WAIT,
                reason="best_ask is missing or non-positive",
            )

        if signal.best_ask > self._rules.max_price:
            return GreyMarketTriggerDecision(
                action=TriggerAction.WAIT,
                reason="best_ask is above max_price",
            )

        notional = self._rules.max_price * self._rules.quantity
        if notional > self._rules.max_notional:
            return GreyMarketTriggerDecision(
                action=TriggerAction.BLOCK,
                reason="order notional would exceed max_notional",
            )

        return GreyMarketTriggerDecision(
            action=TriggerAction.ORDER,
            reason="dark_status TRADING and best_ask within max_price",
            intent=GreyMarketOrderIntent(
                symbol=self._rules.symbol,
                quantity=self._rules.quantity,
                limit_price=self._rules.max_price,
                notional=notional,
                attempt_number=self._attempts + 1,
                remark=self._rules.remark,
            ),
        )

    def record_attempt(self, *, now_monotonic: float) -> None:
        self._attempts += 1
        self._order_times.append(now_monotonic)
        self._prune_order_times(now_monotonic)

    def _minimum_interval_seconds(self, now_monotonic: float) -> float:
        in_opening_burst = (
            self._first_trading_seen_at is not None
            and now_monotonic - self._first_trading_seen_at
            <= self._rules.opening_burst_seconds
        )
        configured_cool_down_ms = (
            self._rules.opening_burst_cool_down_ms
            if in_opening_burst
            else self._rules.cool_down_ms
        )
        return max(configured_cool_down_ms / 1000.0, SAFE_MIN_ORDER_INTERVAL_SECONDS)

    def _prune_order_times(self, now_monotonic: float) -> None:
        while (
            self._order_times
            and now_monotonic - self._order_times[0] >= FUTU_ORDER_LIMIT_WINDOW_SECONDS
        ):
            self._order_times.popleft()


class JsonlEventLogger:
    """Append-only JSONL event writer."""

    def __init__(self, path: Path | str | None = None, *, stream: TextIO | None = None):
        if path is None and stream is None:
            stream = sys.stdout
        self._path = Path(path) if path is not None else None
        self._stream = stream
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self._path.open("a", encoding="utf-8")

    def log(self, event_type: str, **payload: Any) -> None:
        assert self._stream is not None
        record = {
            "ts": _utc_now().isoformat(),
            "event": event_type,
            **payload,
        }
        self._stream.write(json.dumps(record, default=_json_default, ensure_ascii=True))
        self._stream.write("\n")
        self._stream.flush()

    def close(self) -> None:
        if self._path is not None and self._stream is not None:
            self._stream.close()

    def __enter__(self) -> "JsonlEventLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FutuGreyMarketOpenDClient:
    """Thin OpenD adapter for the grey-market open trigger."""

    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self._config = config or RuntimeConfig.from_env()
        validate_runtime_config(self._config)
        self._futu = load_futu_module(self._config)
        self._quote_context = self._futu.OpenQuoteContext(
            host=self._config.futu_host,
            port=self._config.futu_port,
        )
        self._trade_context = None
        self._trade_unlocked = False
        self._subscribed_symbol: str | None = None
        self._push_lock = Lock()
        self._push_handlers_enabled = False
        self._push_quotes_by_symbol: dict[str, dict[str, Any]] = {}
        self._push_books_by_symbol: dict[str, dict[str, Any]] = {}
        self._push_sequence = 0
        self._last_consumed_push_sequence = 0

    def __enter__(self) -> "FutuGreyMarketOpenDClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def subscribe_market(self, symbol: str) -> None:
        broker_symbol = _normalize_symbol(symbol)
        self._subscribed_symbol = broker_symbol
        subtypes = [
            self._futu.SubType.QUOTE,
            self._futu.SubType.ORDER_BOOK,
            self._futu.SubType.TICKER,
        ]
        ret, data = self._quote_context.subscribe(
            [broker_symbol],
            subtypes,
            subscribe_push=True,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"quote subscribe failed: {data}")
        self._install_quote_push_handlers()

    @property
    def supports_push_signals(self) -> bool:
        return self._push_handlers_enabled

    def ensure_trade_context(self, logger: JsonlEventLogger | None = None) -> None:
        if self._trade_context is not None:
            return
        self._trade_context = self._futu.OpenSecTradeContext(
            filter_trdmarket=self._futu.TrdMarket.HK,
            host=self._config.futu_host,
            port=self._config.futu_port,
            security_firm=self._resolve_security_firm(self._config.futu_security_firm),
        )
        self._install_trade_push_handlers(logger)

    def unlock_trade(self, logger: JsonlEventLogger | None = None) -> None:
        self.ensure_trade_context(logger)
        if self._trade_unlocked:
            return
        if not self._config.allow_real_trade:
            raise BrokerConfigurationError(
                "Real trading is disabled. Set FUTU_ALLOW_REAL_TRADE=1 and pass "
                "--real only after completing operational checks."
            )
        if self._config.futu_trade_password is None:
            raise BrokerConfigurationError(
                "FUTU_TRADE_PASSWORD is required for real trading unlock."
            )
        assert self._trade_context is not None
        ret, data = self._trade_context.unlock_trade(self._config.futu_trade_password)
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"unlock_trade failed: {data}")
        self._trade_unlocked = True
        if logger is not None:
            logger.log("trade_unlock", ok=True)

    def read_signal(self, symbol: str) -> GreyMarketSignal:
        broker_symbol = _normalize_symbol(symbol)
        raw_quote = self._get_quote_row(broker_symbol)
        raw_order_book = self._get_order_book(broker_symbol)
        return self._signal_from_payload(
            broker_symbol=broker_symbol,
            raw_quote=raw_quote,
            raw_order_book=raw_order_book,
        )

    def wait_for_push_signal(
        self,
        symbol: str,
        *,
        timeout_seconds: float,
        sleep,
        monotonic,
    ) -> GreyMarketSignal | None:
        if not self.supports_push_signals:
            return None
        broker_symbol = _normalize_symbol(symbol)
        deadline = monotonic() + max(timeout_seconds, 0.0)
        while monotonic() <= deadline:
            with self._push_lock:
                has_new = self._push_sequence > self._last_consumed_push_sequence
                raw_quote = self._push_quotes_by_symbol.get(broker_symbol)
                raw_order_book = self._push_books_by_symbol.get(broker_symbol)
                if has_new and raw_quote and raw_order_book:
                    self._last_consumed_push_sequence = self._push_sequence
                    return self._signal_from_payload(
                        broker_symbol=broker_symbol,
                        raw_quote=dict(raw_quote),
                        raw_order_book=dict(raw_order_book),
                    )
            sleep(0.001)
        return None

    def _signal_from_payload(
        self,
        *,
        broker_symbol: str,
        raw_quote: dict[str, Any],
        raw_order_book: dict[str, Any],
    ) -> GreyMarketSignal:
        ask = _first_book_level(raw_order_book, "Ask")
        bid = _first_book_level(raw_order_book, "Bid")
        orderbook_timestamp = _first_present(
            raw_order_book,
            (
                "svr_recv_time_ask",
                "svr_recv_time_bid",
                "timestamp",
                "time",
            ),
        )
        return GreyMarketSignal(
            symbol=broker_symbol,
            dark_status=str(raw_quote.get("dark_status") or "UNKNOWN"),
            best_bid=_level_price(bid),
            best_ask=_level_price(ask),
            bid_quantity=_level_quantity(bid),
            ask_quantity=_level_quantity(ask),
            orderbook_timestamp=(
                str(orderbook_timestamp) if orderbook_timestamp is not None else None
            ),
            raw_quote=raw_quote,
            raw_order_book=raw_order_book,
        )

    def place_real_limit_buy(self, intent: GreyMarketOrderIntent) -> dict[str, Any]:
        return self.place_real_limit_order(
            GreyMarketRealOrderIntent(
                symbol=intent.symbol,
                side=GreyOrderSide.BUY,
                quantity=intent.quantity,
                limit_price=intent.limit_price,
                role=GreyOrderRole.CORE_BUY,
                source=GreyOrderSource.OPEN_TRIGGER,
                remark=intent.remark,
            )
        )

    def place_real_limit_order(self, intent: GreyMarketRealOrderIntent) -> dict[str, Any]:
        self.unlock_trade()
        assert self._trade_context is not None
        ret, data = self._trade_context.place_order(
            price=float(intent.limit_price),
            qty=float(intent.quantity),
            code=intent.symbol,
            trd_side=self._resolve_order_side(intent.side),
            order_type=self._futu.OrderType.NORMAL,
            trd_env=self._futu.TrdEnv.REAL,
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
            time_in_force=self._futu.TimeInForce.DAY,
            remark=intent.remark,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"place_order failed: {data}")
        return _table_to_payload(data)

    def _resolve_order_side(self, side: GreyOrderSide):
        if side is GreyOrderSide.BUY:
            return self._futu.TrdSide.BUY
        return self._futu.TrdSide.SELL

    def close(self) -> None:
        if self._trade_context is not None:
            self._trade_context.close()
        self._quote_context.close()

    def _get_quote_row(self, broker_symbol: str) -> dict[str, Any]:
        ret, data = self._quote_context.get_stock_quote([broker_symbol])
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"get_stock_quote failed: {data}")
        rows = _rows_from_table(data)
        if not rows:
            raise BrokerResponseError("get_stock_quote returned no rows.")
        return rows[0]

    def _get_order_book(self, broker_symbol: str) -> dict[str, Any]:
        ret, data = self._quote_context.get_order_book(broker_symbol, num=1)
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"get_order_book failed: {data}")
        if not isinstance(data, dict):
            raise BrokerResponseError(
                f"Unsupported order-book payload type: {type(data).__name__}"
            )
        return dict(data)

    def _resolve_security_firm(self, name: str):
        normalized = name.strip().upper()
        try:
            return getattr(self._futu.SecurityFirm, normalized)
        except AttributeError as exc:
            raise BrokerConfigurationError(
                f"Unsupported FUTU_SECURITY_FIRM value: {name!r}"
            ) from exc

    def _install_trade_push_handlers(self, logger: JsonlEventLogger | None) -> None:
        if logger is None or self._trade_context is None:
            return
        if hasattr(self._futu, "TradeOrderHandlerBase"):
            futu = self._futu

            class OrderHandler(futu.TradeOrderHandlerBase):
                def on_recv_rsp(self, rsp_pb):
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == futu.RET_OK:
                        logger.log("order_push", payload=_table_to_payload(data))
                    else:
                        logger.log("error_event", source="order_push", payload=str(data))
                    return ret, data

            self._trade_context.set_handler(OrderHandler())
        if hasattr(self._futu, "TradeDealHandlerBase"):
            futu = self._futu

            class DealHandler(futu.TradeDealHandlerBase):
                def on_recv_rsp(self, rsp_pb):
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == futu.RET_OK:
                        logger.log("fill_event", payload=_table_to_payload(data))
                    else:
                        logger.log("error_event", source="fill_event", payload=str(data))
                    return ret, data

            self._trade_context.set_handler(DealHandler())

    def _install_quote_push_handlers(self) -> None:
        if self._subscribed_symbol is None:
            return
        if not hasattr(self._futu, "StockQuoteHandlerBase"):
            return
        if not hasattr(self._futu, "OrderBookHandlerBase"):
            return

        futu = self._futu
        subscribed_symbol = self._subscribed_symbol

        class QuoteHandler(futu.StockQuoteHandlerBase):
            def __init__(self, parent: "FutuGreyMarketOpenDClient") -> None:
                super().__init__()
                self._parent = parent

            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != futu.RET_OK:
                    return ret, data
                rows = _rows_from_table(data)
                with self._parent._push_lock:
                    for row in rows:
                        symbol = str(row.get("code") or subscribed_symbol).strip().upper()
                        self._parent._push_quotes_by_symbol[symbol] = dict(row)
                        self._parent._push_sequence += 1
                return ret, data

        class OrderBookHandler(futu.OrderBookHandlerBase):
            def __init__(self, parent: "FutuGreyMarketOpenDClient") -> None:
                super().__init__()
                self._parent = parent

            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != futu.RET_OK:
                    return ret, data
                if not isinstance(data, dict):
                    return ret, data
                symbol = str(data.get("code") or subscribed_symbol).strip().upper()
                with self._parent._push_lock:
                    self._parent._push_books_by_symbol[symbol] = dict(data)
                    self._parent._push_sequence += 1
                return ret, data

        self._quote_context.set_handler(QuoteHandler(self))
        self._quote_context.set_handler(OrderBookHandler(self))
        self._push_handlers_enabled = True


def log_signal(logger: JsonlEventLogger, signal: GreyMarketSignal) -> None:
    logger.log(
        "quote_event",
        symbol=signal.symbol,
        dark_status=signal.dark_status,
        observed_at=signal.observed_at,
        raw_quote=signal.raw_quote,
    )
    logger.log(
        "orderbook_event",
        symbol=signal.symbol,
        best_bid=signal.best_bid,
        best_ask=signal.best_ask,
        bid_quantity=signal.bid_quantity,
        ask_quantity=signal.ask_quantity,
        orderbook_timestamp=signal.orderbook_timestamp,
        observed_at=signal.observed_at,
        raw_order_book=signal.raw_order_book,
    )


def run_replay(
    *,
    input_path: Path,
    rules: GreyMarketTriggerRules,
    logger: JsonlEventLogger,
    stdout: TextIO = sys.stdout,
    cost_reducer_dry_run: bool = False,
    core_ratio: Decimal = Decimal("0.5"),
    trading_ratio: Decimal = Decimal("0.5"),
    estimated_roundtrip_cost_bps: Decimal = Decimal("10"),
    safety_buffer_bps: Decimal = Decimal("5"),
    max_spread_bps: Decimal = Decimal("20"),
    min_turnover_to_activate: Decimal = Decimal("0"),
    min_ticks_to_activate: int = 5,
    overextension_vol_multiple: Decimal = Decimal("2.0"),
    high_pullback_vol_multiple: Decimal = Decimal("0.5"),
    rebuy_anchor_vol_band: Decimal = Decimal("1.0"),
    max_sell_total_position_ratio: Decimal = Decimal("0.25"),
    max_round_trips: int = 1,
) -> int:
    trigger = GreyMarketOpenTrigger(rules)
    logical_now = 0.0
    submitted = 0
    state_by_symbol: dict[str, dict[str, Any]] = {}
    adaptive_tracker = IntradayAdaptiveTracker()
    cost_reducer_engine = CostReducerEngine(
        CostReducerRules(
            core_ratio=core_ratio,
            trading_ratio=trading_ratio,
            estimated_roundtrip_cost_bps=estimated_roundtrip_cost_bps,
            safety_buffer_bps=safety_buffer_bps,
            max_spread_bps=max_spread_bps,
            min_turnover_to_activate=min_turnover_to_activate,
            min_ticks_to_activate=min_ticks_to_activate,
            overextension_vol_multiple=overextension_vol_multiple,
            high_pullback_vol_multiple=high_pullback_vol_multiple,
            rebuy_anchor_vol_band=rebuy_anchor_vol_band,
            max_sell_total_position_ratio=max_sell_total_position_ratio,
            max_round_trips=max_round_trips,
        )
    )
    cost_reducer_state = CostReducerState()
    inventory_state = None
    total_sell_intents = 0
    total_rebuy_intents = 0

    with input_path.open("r", encoding="utf-8") as replay_file:
        for line_number, line in enumerate(replay_file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if not _is_market_replay_record(record):
                    continue
                update = signal_update_from_record(record, default_symbol=rules.symbol)
                symbol = update["symbol"]
                state = state_by_symbol.setdefault(symbol, {"symbol": symbol})
                state.update(
                    {
                        key: value
                        for key, value in update.items()
                        if value is not None or key in {"raw_quote", "raw_order_book"}
                    }
                )
                signal = signal_from_record(state, default_symbol=rules.symbol)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.log(
                    "error_event",
                    source="replay",
                    line_number=line_number,
                    message=str(exc),
                )
                continue

            log_signal(logger, signal)
            if cost_reducer_dry_run:
                adaptive_state = adaptive_tracker.update_from_signal(signal)
                if adaptive_state.last_price is not None and inventory_state is None:
                    lot_size = int(
                        float((signal.raw_quote or {}).get("lot_size", 100) or 100)
                    )
                    inventory_state = split_inventory_targets(
                        total_quantity=rules.quantity,
                        lot_size=max(lot_size, 1),
                        core_ratio=core_ratio,
                        trading_ratio=trading_ratio,
                    )
                    inventory_state.seed_opening_inventory(
                        anchor_price=adaptive_state.last_price
                    )
                if inventory_state is not None:
                    reducer_decision = cost_reducer_engine.evaluate(
                        inventory=inventory_state,
                        market=adaptive_state,
                        state=cost_reducer_state,
                    )
                    logger.log("adaptive_market_state", **_dataclass_to_dict(adaptive_state))
                    logger.log("inventory_state", **_dataclass_to_dict(inventory_state))
                    logger.log(
                        "cost_reducer_decision",
                        **_cost_reducer_decision_payload(
                            decision=reducer_decision,
                            market=adaptive_state,
                            inventory=inventory_state,
                        ),
                    )
                    if reducer_decision.action is CostReducerAction.SELL_TRADING:
                        total_sell_intents += 1
                        logger.log(
                            "trading_sell_intent",
                            quantity=reducer_decision.quantity,
                            price=adaptive_state.last_price,
                        )
                        apply_dry_run_fill(
                            decision=reducer_decision,
                            market=adaptive_state,
                            inventory=inventory_state,
                            state=cost_reducer_state,
                            estimated_roundtrip_cost_bps=estimated_roundtrip_cost_bps,
                        )
                    elif reducer_decision.action is CostReducerAction.REBUY_TRADING:
                        total_rebuy_intents += 1
                        logger.log(
                            "trading_rebuy_intent",
                            quantity=reducer_decision.quantity,
                            price=adaptive_state.last_price,
                        )
                        apply_dry_run_fill(
                            decision=reducer_decision,
                            market=adaptive_state,
                            inventory=inventory_state,
                            state=cost_reducer_state,
                            estimated_roundtrip_cost_bps=estimated_roundtrip_cost_bps,
                        )
            decision = trigger.evaluate(signal, now_monotonic=logical_now)
            logger.log(
                "trigger_event",
                action=decision.action.value,
                reason=decision.reason,
                attempt_count=trigger.attempts,
                signal=signal,
                intent=decision.intent,
            )
            if decision.intent is not None:
                logger.log("order_request", dry_run=True, intent=decision.intent)
                stdout.write(
                    _format_would_place_order(decision.intent, source="replay")
                )
                trigger.record_attempt(now_monotonic=logical_now)
                submitted += 1

            logical_now += max(rules.cool_down_ms / 1000.0, 0.001)

    if cost_reducer_dry_run:
        logger.log(
            "cost_reducer_replay_summary",
            **_cost_reducer_replay_summary_payload(
                inventory=inventory_state,
                state=cost_reducer_state,
                total_sell_intents=total_sell_intents,
                total_rebuy_intents=total_rebuy_intents,
            ),
        )

    return submitted


def run_live(
    *,
    rules: GreyMarketTriggerRules,
    logger: JsonlEventLogger,
    real: bool,
    timeout_seconds: float,
    poll_interval_ms: int,
    config: RuntimeConfig | None = None,
    stdout: TextIO = sys.stdout,
    sleep=_sleep,
    monotonic=_monotonic,
    cost_reducer_dry_run: bool = False,
    core_ratio: Decimal = Decimal("0.5"),
    trading_ratio: Decimal = Decimal("0.5"),
    estimated_roundtrip_cost_bps: Decimal = Decimal("10"),
    safety_buffer_bps: Decimal = Decimal("5"),
    max_spread_bps: Decimal = Decimal("20"),
    min_turnover_to_activate: Decimal = Decimal("0"),
    min_ticks_to_activate: int = 5,
    overextension_vol_multiple: Decimal = Decimal("2.0"),
    high_pullback_vol_multiple: Decimal = Decimal("0.5"),
    rebuy_anchor_vol_band: Decimal = Decimal("1.0"),
    max_sell_total_position_ratio: Decimal = Decimal("0.25"),
    max_round_trips: int = 1,
) -> int:
    runtime_config = config or RuntimeConfig.from_env()
    validate_runtime_config(runtime_config)
    if real and not runtime_config.allow_real_trade:
        raise BrokerConfigurationError(
            "Refusing real run because FUTU_ALLOW_REAL_TRADE is not enabled."
        )

    trigger = GreyMarketOpenTrigger(rules)
    submitted = 0
    started_at = monotonic()
    adaptive_tracker = IntradayAdaptiveTracker()
    cost_reducer_engine = CostReducerEngine(
        CostReducerRules(
            core_ratio=core_ratio,
            trading_ratio=trading_ratio,
            estimated_roundtrip_cost_bps=estimated_roundtrip_cost_bps,
            safety_buffer_bps=safety_buffer_bps,
            max_spread_bps=max_spread_bps,
            min_turnover_to_activate=min_turnover_to_activate,
            min_ticks_to_activate=min_ticks_to_activate,
            overextension_vol_multiple=overextension_vol_multiple,
            high_pullback_vol_multiple=high_pullback_vol_multiple,
            rebuy_anchor_vol_band=rebuy_anchor_vol_band,
            max_sell_total_position_ratio=max_sell_total_position_ratio,
            max_round_trips=max_round_trips,
        )
    )
    cost_reducer_state = CostReducerState()
    inventory_state = None

    with FutuGreyMarketOpenDClient(runtime_config) as client:
        client.subscribe_market(rules.symbol)
        if real:
            client.ensure_trade_context(logger)
            client.unlock_trade(logger)

        while monotonic() - started_at < timeout_seconds:
            poll_sleep_seconds = max(poll_interval_ms, 1) / 1000.0
            signal = client.wait_for_push_signal(
                rules.symbol,
                timeout_seconds=poll_sleep_seconds,
                sleep=sleep,
                monotonic=monotonic,
            )
            used_push_signal = signal is not None
            if signal is None:
                signal = client.read_signal(rules.symbol)
            log_signal(logger, signal)
            if cost_reducer_dry_run:
                adaptive_state = adaptive_tracker.update_from_signal(signal)
                if adaptive_state.last_price is not None and inventory_state is None:
                    lot_size = int(
                        float((signal.raw_quote or {}).get("lot_size", 100) or 100)
                    )
                    inventory_state = split_inventory_targets(
                        total_quantity=rules.quantity,
                        lot_size=max(lot_size, 1),
                        core_ratio=core_ratio,
                        trading_ratio=trading_ratio,
                    )
                    inventory_state.seed_opening_inventory(
                        anchor_price=adaptive_state.last_price
                    )

                if inventory_state is not None:
                    decision = cost_reducer_engine.evaluate(
                        inventory=inventory_state,
                        market=adaptive_state,
                        state=cost_reducer_state,
                    )
                    logger.log(
                        "adaptive_market_state",
                        **_dataclass_to_dict(adaptive_state),
                    )
                    logger.log(
                        "inventory_state",
                        **_dataclass_to_dict(inventory_state),
                    )
                    logger.log(
                        "cost_reducer_decision",
                        **_cost_reducer_decision_payload(
                            decision=decision,
                            market=adaptive_state,
                            inventory=inventory_state,
                        ),
                    )
                    if decision.action is CostReducerAction.SELL_TRADING:
                        logger.log(
                            "trading_sell_intent",
                            quantity=decision.quantity,
                            price=adaptive_state.last_price,
                        )
                        apply_dry_run_fill(
                            decision=decision,
                            market=adaptive_state,
                            inventory=inventory_state,
                            state=cost_reducer_state,
                            estimated_roundtrip_cost_bps=estimated_roundtrip_cost_bps,
                        )
                    elif decision.action is CostReducerAction.REBUY_TRADING:
                        logger.log(
                            "trading_rebuy_intent",
                            quantity=decision.quantity,
                            price=adaptive_state.last_price,
                        )
                        apply_dry_run_fill(
                            decision=decision,
                            market=adaptive_state,
                            inventory=inventory_state,
                            state=cost_reducer_state,
                            estimated_roundtrip_cost_bps=estimated_roundtrip_cost_bps,
                        )
            now = monotonic()
            decision = trigger.evaluate(signal, now_monotonic=now)
            logger.log(
                "trigger_event",
                action=decision.action.value,
                reason=decision.reason,
                attempt_count=trigger.attempts,
                signal=signal,
                intent=decision.intent,
            )

            if decision.intent is not None:
                logger.log("order_request", dry_run=not real, intent=decision.intent)
                if real:
                    response = client.place_real_limit_buy(decision.intent)
                    logger.log("order_response", ok=True, payload=response)
                else:
                    stdout.write(
                        _format_would_place_order(decision.intent, source="live")
                    )
                trigger.record_attempt(now_monotonic=now)
                submitted += 1

            if decision.action is TriggerAction.BLOCK:
                break
            if not used_push_signal:
                sleep(poll_sleep_seconds)

    return submitted


def signal_from_record(
    record: dict[str, Any],
    *,
    default_symbol: str,
) -> GreyMarketSignal:
    update = signal_update_from_record(record, default_symbol=default_symbol)
    dark_status = update.get("dark_status")
    if dark_status is None:
        raise ValueError("Replay record is missing dark_status.")

    return GreyMarketSignal(
        symbol=str(update["symbol"]),
        dark_status=str(dark_status),
        best_bid=update.get("best_bid"),
        best_ask=update.get("best_ask"),
        bid_quantity=_optional_int(update.get("bid_quantity")),
        ask_quantity=_optional_int(update.get("ask_quantity")),
        orderbook_timestamp=_optional_str(update.get("orderbook_timestamp")),
        raw_quote=(
            update.get("raw_quote")
            if isinstance(update.get("raw_quote"), dict)
            else None
        ),
        raw_order_book=(
            update.get("raw_order_book")
            if isinstance(update.get("raw_order_book"), dict)
            else None
        ),
    )


def signal_update_from_record(
    record: dict[str, Any],
    *,
    default_symbol: str,
) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        source = {**record, **payload}
    else:
        source = dict(record)

    if "signal" in source and isinstance(source["signal"], dict):
        source = {**source, **source["signal"]}

    raw_quote = source.get("raw_quote")
    raw_order_book = source.get("raw_order_book")
    if raw_quote is None and isinstance(source.get("quote"), dict):
        raw_quote = source["quote"]
    if raw_order_book is None and isinstance(source.get("order_book"), dict):
        raw_order_book = source["order_book"]

    dark_status = source.get("dark_status")
    if dark_status is None and isinstance(raw_quote, dict):
        dark_status = raw_quote.get("dark_status")
    best_ask = source.get("best_ask")
    best_bid = source.get("best_bid")
    ask_quantity = source.get("ask_quantity")
    bid_quantity = source.get("bid_quantity")
    if isinstance(raw_order_book, dict):
        ask = _first_book_level(raw_order_book, "Ask")
        bid = _first_book_level(raw_order_book, "Bid")
        best_ask = best_ask if best_ask is not None else _level_price(ask)
        best_bid = best_bid if best_bid is not None else _level_price(bid)
        ask_quantity = ask_quantity if ask_quantity is not None else _level_quantity(ask)
        bid_quantity = bid_quantity if bid_quantity is not None else _level_quantity(bid)

    return {
        "symbol": str(source.get("symbol") or source.get("code") or default_symbol),
        "dark_status": str(dark_status) if dark_status is not None else None,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_quantity": _optional_int(bid_quantity),
        "ask_quantity": _optional_int(ask_quantity),
        "orderbook_timestamp": _optional_str(
            source.get("orderbook_timestamp")
            or source.get("order_book_timestamp")
            or (
                _first_present(
                    raw_order_book,
                    ("svr_recv_time_ask", "svr_recv_time_bid", "timestamp", "time"),
                )
                if isinstance(raw_order_book, dict)
                else None
            )
        ),
        "raw_quote": raw_quote if isinstance(raw_quote, dict) else None,
        "raw_order_book": raw_order_book if isinstance(raw_order_book, dict) else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grey-market open trigger for Futu OpenD. Defaults to dry-run."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    live = subparsers.add_parser("live", help="Run against local OpenD.")
    _add_common_rule_args(live)
    live.add_argument(
        "--real",
        action="store_true",
        help="Actually call place_order. Also requires FUTU_ALLOW_REAL_TRADE=1.",
    )
    live.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
        help="Stop the live loop after this many seconds.",
    )
    live.add_argument(
        "--poll-interval-ms",
        type=int,
        default=50,
        help="Snapshot poll interval after subscriptions are active.",
    )
    replay = subparsers.add_parser(
        "replay",
        help="Replay historical JSONL quote/order-book events in dry-run mode.",
    )
    replay.add_argument("input_path", type=Path, help="Input JSONL event file.")
    _add_common_rule_args(replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    execution_mode = _resolve_execution_mode(args)
    rules = GreyMarketTriggerRules(
        symbol=args.symbol,
        quantity=args.quantity,
        max_price=args.max_price,
        max_qty=args.max_qty,
        max_notional=args.max_notional,
        max_order_attempts=args.max_order_attempts,
        cool_down_ms=args.cool_down_ms,
        opening_burst_seconds=args.opening_burst_seconds,
        opening_burst_cool_down_ms=args.opening_burst_cool_down_ms,
        kill_switch_file=args.kill_switch_file,
        remark=args.remark,
    )

    try:
        with JsonlEventLogger(args.log_file) as logger:
            if args.print_config:
                summary = _config_summary(args=args, rules=rules, execution_mode=execution_mode)
                print(json.dumps(summary, ensure_ascii=True, default=_json_default, indent=2))
                logger.log("grey_open_config_summary", **summary)
            if args.command == "replay":
                submitted = run_replay(
                    input_path=args.input_path,
                    rules=rules,
                    logger=logger,
                    cost_reducer_dry_run=bool(getattr(args, "cost_reducer_dry_run", False)),
                    **_cost_reducer_kwargs_from_args(args),
                )
            else:
                submitted = run_live(
                    rules=rules,
                    logger=logger,
                    real=args.real,
                    timeout_seconds=args.timeout_seconds,
                    poll_interval_ms=args.poll_interval_ms,
                    cost_reducer_dry_run=bool(getattr(args, "cost_reducer_dry_run", False)),
                    **_cost_reducer_kwargs_from_args(args),
                )
    except Exception as exc:
        with JsonlEventLogger(args.log_file) as logger:
            logger.log("error_event", source=args.command, message=str(exc))
        raise

    print(json.dumps({"submitted_or_would_submit": submitted}, ensure_ascii=True))
    return 0


def _add_common_rule_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("symbol", help="HK symbol, for example HK.09868 or 09868.")
    parser.add_argument("--quantity", type=int, required=True, help="Target buy size.")
    parser.add_argument("--max-price", required=True, help="Limit price cap.")
    parser.add_argument("--max-qty", type=int, required=True, help="Hard quantity cap.")
    parser.add_argument(
        "--max-notional",
        required=True,
        help="Hard notional cap using max-price * quantity.",
    )
    parser.add_argument(
        "--max-order-attempts",
        type=int,
        default=3,
        help="Maximum order attempts for this run. Must stay below OpenD limits.",
    )
    parser.add_argument(
        "--cool-down-ms",
        type=int,
        default=300,
        help="Cooldown between order attempts. A 50ms minimum is enforced.",
    )
    parser.add_argument(
        "--opening-burst-seconds",
        type=float,
        default=0.0,
        help="Duration of the first TRADING burst window in seconds.",
    )
    parser.add_argument(
        "--opening-burst-cool-down-ms",
        type=int,
        default=50,
        help="Cooldown used during the opening burst window.",
    )
    parser.add_argument(
        "--kill-switch-file",
        type=Path,
        default=None,
        help="If this file exists, order generation is blocked.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs/grey_open.jsonl"),
        help="Append JSONL events here.",
    )
    parser.add_argument(
        "--remark",
        default="grey_open_v1",
        help="Broker order remark used for real submissions.",
    )
    parser.add_argument(
        "--cost-reducer-dry-run",
        action="store_true",
        help="Enable dry-run cost reducer logging only (no sell/rebuy order placement).",
    )
    parser.add_argument("--core-ratio", default="0.5")
    parser.add_argument("--trading-ratio", default="0.5")
    parser.add_argument("--estimated-roundtrip-cost-bps", default="10")
    parser.add_argument("--safety-buffer-bps", default="5")
    parser.add_argument("--max-spread-bps", default="20")
    parser.add_argument("--min-turnover-to-activate", default="0")
    parser.add_argument("--min-ticks-to-activate", type=int, default=5)
    parser.add_argument("--overextension-vol-multiple", default="2.0")
    parser.add_argument("--high-pullback-vol-multiple", default="0.5")
    parser.add_argument("--rebuy-anchor-vol-band", default="1.0")
    parser.add_argument("--max-sell-total-position-ratio", default="0.25")
    parser.add_argument("--max-round-trips", type=int, default=1)
    parser.add_argument(
        "--execution-mode",
        choices=[mode.value for mode in ExecutionMode],
        default=None,
        help="Explicit execution mode. Defaults to REPLAY for replay, dry-run for live.",
    )
    parser.add_argument(
        "--manual-approval-required",
        action="store_true",
        help="Require manual approval for real cost reducer sell/rebuy intents.",
    )
    parser.add_argument(
        "--enable-auto-cost-reducer",
        action="store_true",
        help="Experimental flag only; auto real cost reducer remains disabled unless server config allows it.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print a JSON dry-run/safety configuration summary before running.",
    )


def _cost_reducer_kwargs_from_args(args) -> dict[str, Any]:
    return {
        "core_ratio": Decimal(str(getattr(args, "core_ratio", "0.5"))),
        "trading_ratio": Decimal(str(getattr(args, "trading_ratio", "0.5"))),
        "estimated_roundtrip_cost_bps": Decimal(
            str(getattr(args, "estimated_roundtrip_cost_bps", "10"))
        ),
        "safety_buffer_bps": Decimal(str(getattr(args, "safety_buffer_bps", "5"))),
        "max_spread_bps": Decimal(str(getattr(args, "max_spread_bps", "20"))),
        "min_turnover_to_activate": Decimal(
            str(getattr(args, "min_turnover_to_activate", "0"))
        ),
        "min_ticks_to_activate": int(getattr(args, "min_ticks_to_activate", 5)),
        "overextension_vol_multiple": Decimal(
            str(getattr(args, "overextension_vol_multiple", "2.0"))
        ),
        "high_pullback_vol_multiple": Decimal(
            str(getattr(args, "high_pullback_vol_multiple", "0.5"))
        ),
        "rebuy_anchor_vol_band": Decimal(
            str(getattr(args, "rebuy_anchor_vol_band", "1.0"))
        ),
        "max_sell_total_position_ratio": Decimal(
            str(getattr(args, "max_sell_total_position_ratio", "0.25"))
        ),
        "max_round_trips": int(getattr(args, "max_round_trips", 1)),
    }


def _resolve_execution_mode(args) -> ExecutionMode:
    explicit = getattr(args, "execution_mode", None)
    if explicit:
        return ExecutionMode(explicit)
    if args.command == "replay":
        return ExecutionMode.REPLAY
    if getattr(args, "real", False):
        return ExecutionMode.LIVE_REAL_BUY_ONLY
    return ExecutionMode.LIVE_DRY_RUN


def _config_summary(*, args, rules: GreyMarketTriggerRules, execution_mode: ExecutionMode) -> dict[str, Any]:
    kwargs = _cost_reducer_kwargs_from_args(args)
    return {
        "symbol": rules.symbol,
        "quantity": rules.quantity,
        "core_trading_split": {
            "core_ratio": kwargs["core_ratio"],
            "trading_ratio": kwargs["trading_ratio"],
        },
        "max_price": rules.max_price,
        "max_qty": rules.max_qty,
        "max_notional": rules.max_notional,
        "execution_mode": execution_mode.value,
        "real_order_requested": bool(getattr(args, "real", False)),
        "cost_reducer_enabled": bool(getattr(args, "cost_reducer_dry_run", False)),
        "cost_reducer_params": kwargs,
        "safety_gates": {
            "default_dry_run": True,
            "kill_switch_file": rules.kill_switch_file,
            "max_order_attempts": rules.max_order_attempts,
            "cool_down_ms": rules.cool_down_ms,
            "manual_approval_required": bool(getattr(args, "manual_approval_required", False)),
            "enable_auto_cost_reducer": bool(getattr(args, "enable_auto_cost_reducer", False)),
        },
    }


def _format_would_place_order(intent: GreyMarketOrderIntent, *, source: str) -> str:
    return (
        "would_place_order "
        f"source={source} "
        f"attempt={intent.attempt_number} "
        f"code={intent.symbol} "
        f"side=BUY "
        f"qty={intent.quantity} "
        f"order_type=NORMAL "
        f"price={intent.limit_price} "
        f"time_in_force=DAY "
        f"notional={intent.notional}\n"
    )


def _dataclass_to_dict(value) -> dict[str, Any]:
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _json_default(getattr(value, key))
            for key in value.__dataclass_fields__
        }
    return {"value": _json_default(value)}


def _cost_reducer_decision_payload(
    *,
    decision,
    market,
    inventory,
) -> dict[str, Any]:
    return {
        "action": decision.action.value,
        "quantity": decision.quantity,
        "reason": decision.reason,
        "last_price": _json_default(market.last_price),
        "opening_vwap": _json_default(market.opening_vwap),
        "rolling_vwap": _json_default(market.rolling_vwap),
        "realized_vol": _json_default(market.realized_vol),
        "rolling_high": _json_default(market.rolling_high),
        "orderbook_imbalance": _json_default(market.orderbook_imbalance),
        "spread_bps": _json_default(market.spread_bps),
        "current_position": inventory.current_position,
        "trading_available_to_sell": inventory.trading_available_to_sell,
        "trading_available_to_rebuy": inventory.trading_available_to_rebuy,
        "economic_cost_basis": _json_default(inventory.economic_cost_basis),
    }


def _cost_reducer_replay_summary_payload(
    *,
    inventory,
    state: CostReducerState,
    total_sell_intents: int,
    total_rebuy_intents: int,
) -> dict[str, Any]:
    return {
        "total_sell_intents": total_sell_intents,
        "total_rebuy_intents": total_rebuy_intents,
        "final_current_position": (
            inventory.current_position if inventory is not None else None
        ),
        "final_economic_cost_basis": (
            _json_default(inventory.economic_cost_basis)
            if inventory is not None
            else None
        ),
        "final_trading_qty_sold": (
            inventory.trading_qty_sold if inventory is not None else None
        ),
        "final_trading_qty_rebought": (
            inventory.trading_qty_rebought if inventory is not None else None
        ),
        "round_trips_completed": state.round_trips_completed,
        "last_sell_price": _json_default(state.last_sell_price),
    }


def _is_market_replay_record(record: dict[str, Any]) -> bool:
    event = record.get("event")
    if event in {"quote_event", "orderbook_event"}:
        return True
    if event is not None and event not in {"quote_event", "orderbook_event"}:
        return False
    return any(
        key in record
        for key in (
            "dark_status",
            "best_bid",
            "best_ask",
            "raw_quote",
            "raw_order_book",
            "quote",
            "order_book",
            "signal",
        )
    )


def _rows_from_table(table) -> list[dict[str, Any]]:
    if hasattr(table, "to_dict"):
        return [dict(row) for row in table.to_dict("records")]
    if isinstance(table, list):
        return [dict(row) for row in table]
    if isinstance(table, dict):
        return [dict(table)]
    raise BrokerResponseError(
        f"Unsupported table payload type: {type(table).__name__}"
    )


def _table_to_payload(table) -> Any:
    if hasattr(table, "to_dict"):
        return table.to_dict("records")
    return table


def _first_book_level(raw_order_book: dict[str, Any], side: str) -> Any:
    levels = raw_order_book.get(side) or raw_order_book.get(side.lower())
    if not levels:
        return None
    return levels[0]


def _level_price(level: Any) -> Decimal | None:
    if level is None:
        return None
    if isinstance(level, dict):
        return _to_decimal(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return _to_decimal(level[0])
    return None


def _level_quantity(level: Any) -> int | None:
    if level is None:
        return None
    value = None
    if isinstance(level, dict):
        value = level.get("quantity", level.get("qty"))
    elif isinstance(level, (list, tuple)) and len(level) > 1:
        value = level[1]
    return _optional_int(value)


def _optional_int(value: Any) -> int | None:
    if value in {None, "", "N/A"}:
        return None
    return int(float(value))


def _optional_str(value: Any) -> str | None:
    if value in {None, "", "N/A"}:
        return None
    return str(value)


def _first_present(source: dict[str, Any] | None, keys: Iterable[str]) -> Any:
    if source is None:
        return None
    for key in keys:
        value = source.get(key)
        if value not in {None, "", "N/A"}:
            return value
    return None


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            field: getattr(value, field)
            for field in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
