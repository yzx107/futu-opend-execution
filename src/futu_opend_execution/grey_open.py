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
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Any, Iterable, TextIO

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import (
    BrokerConfigurationError,
    BrokerResponseError,
)
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.risk import ExecutionValidationError, validate_runtime_config


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


class GreyMarketOpenTrigger:
    """Stateful risk gate for open-triggered orders."""

    def __init__(self, rules: GreyMarketTriggerRules) -> None:
        self._rules = rules
        self._attempts = 0
        self._order_times: deque[float] = deque()

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
            if elapsed < self._minimum_interval_seconds:
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

    @property
    def _minimum_interval_seconds(self) -> float:
        return max(self._rules.cool_down_ms / 1000.0, SAFE_MIN_ORDER_INTERVAL_SECONDS)

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

    def __enter__(self) -> "FutuGreyMarketOpenDClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def subscribe_market(self, symbol: str) -> None:
        broker_symbol = _normalize_symbol(symbol)
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
        self.unlock_trade()
        assert self._trade_context is not None
        ret, data = self._trade_context.place_order(
            price=float(intent.limit_price),
            qty=float(intent.quantity),
            code=intent.symbol,
            trd_side=self._futu.TrdSide.BUY,
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
) -> int:
    trigger = GreyMarketOpenTrigger(rules)
    logical_now = 0.0
    submitted = 0
    state_by_symbol: dict[str, dict[str, Any]] = {}

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

    with FutuGreyMarketOpenDClient(runtime_config) as client:
        client.subscribe_market(rules.symbol)
        client.ensure_trade_context(logger)
        if real:
            client.unlock_trade(logger)

        while monotonic() - started_at < timeout_seconds:
            signal = client.read_signal(rules.symbol)
            log_signal(logger, signal)
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
            sleep(max(poll_interval_ms, 1) / 1000.0)

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
    rules = GreyMarketTriggerRules(
        symbol=args.symbol,
        quantity=args.quantity,
        max_price=args.max_price,
        max_qty=args.max_qty,
        max_notional=args.max_notional,
        max_order_attempts=args.max_order_attempts,
        cool_down_ms=args.cool_down_ms,
        kill_switch_file=args.kill_switch_file,
        remark=args.remark,
    )

    try:
        with JsonlEventLogger(args.log_file) as logger:
            if args.command == "replay":
                submitted = run_replay(
                    input_path=args.input_path,
                    rules=rules,
                    logger=logger,
                )
            else:
                submitted = run_live(
                    rules=rules,
                    logger=logger,
                    real=args.real,
                    timeout_seconds=args.timeout_seconds,
                    poll_interval_ms=args.poll_interval_ms,
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
