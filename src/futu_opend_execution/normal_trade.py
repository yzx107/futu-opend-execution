"""Normal-session trading CLI for Futu OpenD.

The default mode is dry-run. Real orders require both FUTU_ALLOW_REAL_TRADE=1
and the --real flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from futu_opend_execution._compat import StrEnum
from pathlib import Path
from typing import Any, TextIO

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import (
    BrokerConfigurationError,
    BrokerResponseError,
)
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.grey_open import JsonlEventLogger
from futu_opend_execution.risk import ExecutionValidationError, validate_runtime_config


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


class NormalTradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class NormalOrderType(StrEnum):
    NORMAL = "NORMAL"
    MARKET = "MARKET"


class NormalQuantityMode(StrEnum):
    LOTS = "LOTS"
    SHARES = "SHARES"


TERMINAL_ORDER_STATUSES = {
    "FILLED_ALL",
    "FAILED",
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "DISABLED",
    "DELETED",
    "FILL_CANCELLED",
}


@dataclass(frozen=True, slots=True)
class NormalTradeQuote:
    symbol: str
    lot_size: int
    best_bid: Decimal | None
    best_ask: Decimal | None
    last_price: Decimal | None
    raw_quote: dict[str, Any] | None = None
    raw_basic: dict[str, Any] | None = None
    raw_order_book: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(self, "best_bid", _to_decimal(self.best_bid))
        object.__setattr__(self, "best_ask", _to_decimal(self.best_ask))
        object.__setattr__(self, "last_price", _to_decimal(self.last_price))
        if self.lot_size <= 0:
            raise ExecutionValidationError("lot_size must be positive.")
        if self.raw_quote is not None:
            object.__setattr__(self, "raw_quote", dict(self.raw_quote))
        if self.raw_basic is not None:
            object.__setattr__(self, "raw_basic", dict(self.raw_basic))
        if self.raw_order_book is not None:
            object.__setattr__(self, "raw_order_book", dict(self.raw_order_book))


@dataclass(frozen=True, slots=True)
class NormalTradeIntent:
    symbol: str
    side: NormalTradeSide | str
    order_type: NormalOrderType | str
    quantity: int
    limit_price: Decimal | None
    risk_price: Decimal
    max_notional: Decimal
    remark: str = "normal_trade_v1"

    def __post_init__(self) -> None:
        side = self.side if isinstance(self.side, NormalTradeSide) else NormalTradeSide(str(self.side).upper())
        order_type = (
            self.order_type
            if isinstance(self.order_type, NormalOrderType)
            else NormalOrderType(str(self.order_type).upper())
        )
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "limit_price", _to_decimal(self.limit_price))
        object.__setattr__(self, "risk_price", _to_decimal(self.risk_price))
        object.__setattr__(self, "max_notional", _to_decimal(self.max_notional))
        self.validate()

    @property
    def notional(self) -> Decimal:
        return self.risk_price * self.quantity

    @property
    def broker_price(self) -> Decimal:
        if self.order_type is NormalOrderType.MARKET:
            return Decimal("0")
        assert self.limit_price is not None
        return self.limit_price

    def validate(self) -> None:
        if not self.symbol:
            raise ExecutionValidationError("symbol must not be empty.")
        if self.quantity <= 0:
            raise ExecutionValidationError("quantity must be positive.")
        if self.order_type is NormalOrderType.NORMAL:
            if self.limit_price is None or self.limit_price <= 0:
                raise ExecutionValidationError(
                    "limit_price must be positive for NORMAL orders."
                )
        if self.risk_price <= 0:
            raise ExecutionValidationError("risk_price must be positive.")
        if self.max_notional <= 0:
            raise ExecutionValidationError("max_notional must be positive.")
        if self.notional > self.max_notional:
            raise ExecutionValidationError(
                "limit_price * quantity exceeds max_notional."
            )


class FutuNormalTradeClient:
    """OpenD client for normal limit-order workflows."""

    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self._config = config or RuntimeConfig.from_env()
        validate_runtime_config(self._config)
        self._futu = load_futu_module(self._config)
        self._quote_context = self._futu.OpenQuoteContext(
            host=self._config.futu_host,
            port=self._config.futu_port,
        )
        self._trade_context = self._futu.OpenSecTradeContext(
            filter_trdmarket=self._futu.TrdMarket.HK,
            host=self._config.futu_host,
            port=self._config.futu_port,
            security_firm=self._resolve_security_firm(self._config.futu_security_firm),
        )
        self._trade_unlocked = False

    def __enter__(self) -> "FutuNormalTradeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def read_quote(self, symbol: str) -> NormalTradeQuote:
        broker_symbol = _normalize_symbol(symbol)
        self._subscribe_quote(broker_symbol)
        raw_basic = self._get_basic_row(broker_symbol)
        raw_quote = self._get_quote_row(broker_symbol)
        raw_order_book = self._get_order_book(broker_symbol)
        ask = _first_book_level(raw_order_book, "Ask")
        bid = _first_book_level(raw_order_book, "Bid")
        return NormalTradeQuote(
            symbol=broker_symbol,
            lot_size=int(float(raw_basic.get("lot_size", 0) or 0)),
            best_bid=_level_price(bid),
            best_ask=_level_price(ask),
            last_price=_to_decimal(raw_quote.get("last_price")),
            raw_quote=raw_quote,
            raw_basic=raw_basic,
            raw_order_book=raw_order_book,
        )

    def _subscribe_quote(self, broker_symbol: str) -> None:
        ret, data = self._quote_context.subscribe(
            [broker_symbol],
            [self._futu.SubType.QUOTE, self._futu.SubType.ORDER_BOOK],
            subscribe_push=False,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"quote subscribe failed: {data}")

    def unlock_trade(self) -> None:
        if self._trade_unlocked:
            return
        if not self._config.allow_real_trade:
            raise BrokerConfigurationError(
                "Real trading is disabled. Set FUTU_ALLOW_REAL_TRADE=1 and pass --real."
            )
        if not self._config.futu_trade_password:
            raise BrokerConfigurationError(
                "FUTU_TRADE_PASSWORD is required for real trading unlock."
            )
        ret, data = self._trade_context.unlock_trade(self._config.futu_trade_password)
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"unlock_trade failed: {data}")
        self._trade_unlocked = True

    def place_limit_order(self, intent: NormalTradeIntent) -> Any:
        return self.place_order(intent)

    def place_order(self, intent: NormalTradeIntent) -> Any:
        self.unlock_trade()
        ret, data = self._trade_context.place_order(
            price=float(intent.broker_price),
            qty=float(intent.quantity),
            code=intent.symbol,
            trd_side=self._resolve_side(intent.side),
            order_type=self._resolve_order_type(intent.order_type),
            trd_env=self._futu.TrdEnv.REAL,
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
            time_in_force=self._futu.TimeInForce.DAY,
            remark=intent.remark,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"place_order failed: {data}")
        return _table_to_payload(data)

    def query_order(self, *, order_id: str, symbol: str) -> Any:
        ret, data = self._trade_context.order_list_query(
            order_id=order_id,
            code=_normalize_symbol(symbol),
            trd_env=self._futu.TrdEnv.REAL,
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
            refresh_cache=True,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"order_list_query failed: {data}")
        return _table_to_payload(data)

    def wait_for_terminal_order(
        self,
        *,
        order_id: str,
        symbol: str,
        timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 0.5,
    ) -> list[Any]:
        timeline: list[Any] = []
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() <= deadline:
            snapshot = self.query_order(order_id=order_id, symbol=symbol)
            timeline.append(snapshot)
            first = snapshot[0] if isinstance(snapshot, list) and snapshot else {}
            status = str(first.get("order_status", "")).upper()
            if status in TERMINAL_ORDER_STATUSES:
                break
            time.sleep(poll_interval_seconds)
        return timeline

    def close(self) -> None:
        self._trade_context.close()
        self._quote_context.close()

    def _get_basic_row(self, broker_symbol: str) -> dict[str, Any]:
        ret, data = self._quote_context.get_stock_basicinfo(
            self._futu.Market.HK,
            self._futu.SecurityType.STOCK,
            [broker_symbol],
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"get_stock_basicinfo failed: {data}")
        rows = _rows_from_table(data)
        if not rows:
            raise BrokerResponseError("get_stock_basicinfo returned no rows.")
        return rows[0]

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

    def _resolve_side(self, side: NormalTradeSide):
        if side is NormalTradeSide.BUY:
            return self._futu.TrdSide.BUY
        return self._futu.TrdSide.SELL

    def _resolve_order_type(self, order_type: NormalOrderType):
        if order_type is NormalOrderType.MARKET:
            return self._futu.OrderType.MARKET
        return self._futu.OrderType.NORMAL

    def _resolve_security_firm(self, name: str):
        normalized = name.strip().upper()
        try:
            return getattr(self._futu.SecurityFirm, normalized)
        except AttributeError as exc:
            raise BrokerConfigurationError(
                f"Unsupported FUTU_SECURITY_FIRM value: {name!r}"
            ) from exc


def build_one_lot_intent(
    *,
    quote: NormalTradeQuote,
    side: NormalTradeSide | str,
    limit_price: Decimal | str,
    max_notional: Decimal | str,
    order_type: NormalOrderType | str = NormalOrderType.NORMAL,
    remark: str = "normal_trade_v1",
) -> NormalTradeIntent:
    return build_normal_trade_intent(
        quote=quote,
        side=side,
        order_type=order_type,
        quantity_mode=NormalQuantityMode.LOTS,
        lots=1,
        shares=None,
        limit_price=limit_price,
        max_notional=max_notional,
        remark=remark,
    )


def build_normal_trade_intent(
    *,
    quote: NormalTradeQuote,
    side: NormalTradeSide | str,
    order_type: NormalOrderType | str,
    quantity_mode: NormalQuantityMode | str,
    lots: int | None,
    shares: int | None,
    limit_price: Decimal | str | None,
    max_notional: Decimal | str,
    remark: str = "normal_trade_v1",
) -> NormalTradeIntent:
    resolved_side = (
        side if isinstance(side, NormalTradeSide) else NormalTradeSide(str(side).upper())
    )
    resolved_order_type = (
        order_type
        if isinstance(order_type, NormalOrderType)
        else NormalOrderType(str(order_type).upper())
    )
    resolved_quantity_mode = (
        quantity_mode
        if isinstance(quantity_mode, NormalQuantityMode)
        else NormalQuantityMode(str(quantity_mode).upper())
    )
    if resolved_quantity_mode is NormalQuantityMode.LOTS:
        if lots is None or lots <= 0:
            raise ExecutionValidationError("lots must be positive.")
        quantity = lots * quote.lot_size
    else:
        if shares is None or shares <= 0:
            raise ExecutionValidationError("shares must be positive.")
        quantity = shares

    normalized_limit = _to_decimal(limit_price)
    risk_price = _risk_price_for_order(
        quote=quote,
        side=resolved_side,
        order_type=resolved_order_type,
        limit_price=normalized_limit,
    )
    return NormalTradeIntent(
        symbol=quote.symbol,
        side=resolved_side,
        order_type=resolved_order_type,
        quantity=quantity,
        limit_price=normalized_limit,
        risk_price=risk_price,
        max_notional=max_notional,
        remark=remark,
    )


def run_normal_trade(
    *,
    symbol: str,
    side: NormalTradeSide,
    limit_price: Decimal | str,
    max_notional: Decimal | str,
    real: bool,
    log_file: Path,
    remark: str,
    order_type: NormalOrderType = NormalOrderType.NORMAL,
    quantity_mode: NormalQuantityMode = NormalQuantityMode.LOTS,
    lots: int | None = 1,
    shares: int | None = None,
    wait_terminal: bool = True,
    config: RuntimeConfig | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    runtime_config = config or RuntimeConfig.from_env()
    if real and not runtime_config.allow_real_trade:
        raise BrokerConfigurationError(
            "Refusing real order because FUTU_ALLOW_REAL_TRADE is not enabled."
        )

    with JsonlEventLogger(log_file) as logger:
        with FutuNormalTradeClient(runtime_config) as client:
            quote = client.read_quote(symbol)
            logger.log("normal_trade_quote", quote=quote)
            intent = build_normal_trade_intent(
                quote=quote,
                side=side,
                order_type=order_type,
                quantity_mode=quantity_mode,
                lots=lots,
                shares=shares,
                limit_price=limit_price,
                max_notional=max_notional,
                remark=remark,
            )
            logger.log("normal_trade_order_request", dry_run=not real, intent=intent)
            if not real:
                stdout.write(_format_would_place_order(intent, quote=quote))
                return 0

            response = client.place_order(intent)
            logger.log("normal_trade_order_response", ok=True, payload=response)
            timeline = []
            order_id = _extract_order_id(response)
            if wait_terminal and order_id:
                timeline = client.wait_for_terminal_order(
                    order_id=order_id,
                    symbol=intent.symbol,
                )
                logger.log("normal_trade_order_timeline", payload=timeline)
            stdout.write(
                json.dumps(
                    {
                        "submitted": True,
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "order_type": intent.order_type.value,
                        "quantity": intent.quantity,
                        "price": str(intent.broker_price),
                        "notional": str(intent.notional),
                        "response": response,
                        "timeline": timeline,
                    },
                    ensure_ascii=True,
                    default=_json_default,
                )
                + "\n"
            )
            return 0


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normal-session one-lot limit order CLI. Defaults to dry-run."
    )
    parser.add_argument("symbol", help="HK symbol, for example HK.00700 or 00700.")
    parser.add_argument(
        "--side",
        choices=[side.value for side in NormalTradeSide],
        default=NormalTradeSide.BUY.value,
        help="Order side.",
    )
    parser.add_argument(
        "--order-type",
        choices=[order_type.value for order_type in NormalOrderType],
        default=NormalOrderType.NORMAL.value,
        help="Order type.",
    )
    parser.add_argument("--limit-price", default=None, help="Limit order price.")
    parser.add_argument(
        "--quantity-mode",
        choices=[mode.value for mode in NormalQuantityMode],
        default=NormalQuantityMode.LOTS.value,
        help="Use board lots or raw shares.",
    )
    parser.add_argument("--lots", type=int, default=1, help="Board-lot count.")
    parser.add_argument("--shares", type=int, default=None, help="Raw share quantity.")
    parser.add_argument(
        "--max-notional",
        required=True,
        help="Hard notional cap. One-lot quantity * limit price must not exceed this.",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Actually call place_order. Also requires FUTU_ALLOW_REAL_TRADE=1.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs/normal_trade.jsonl"),
        help="Append JSONL events here.",
    )
    parser.add_argument(
        "--remark",
        default="normal_trade_v1",
        help="Broker order remark.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    return run_normal_trade(
        symbol=args.symbol,
        side=NormalTradeSide(args.side),
        order_type=NormalOrderType(args.order_type),
        quantity_mode=NormalQuantityMode(args.quantity_mode),
        lots=args.lots,
        shares=args.shares,
        limit_price=args.limit_price,
        max_notional=args.max_notional,
        real=args.real,
        log_file=args.log_file,
        remark=args.remark,
    )


def _format_would_place_order(intent: NormalTradeIntent, *, quote: NormalTradeQuote) -> str:
    return (
        "would_place_order "
        f"code={intent.symbol} "
        f"side={intent.side.value} "
        f"qty={intent.quantity} "
        f"lot_size={quote.lot_size} "
        f"order_type={intent.order_type.value} "
        f"price={intent.broker_price} "
        f"time_in_force=DAY "
        f"risk_notional={intent.notional} "
        f"best_bid={quote.best_bid} "
        f"best_ask={quote.best_ask} "
        f"last_price={quote.last_price}\n"
    )


def _risk_price_for_order(
    *,
    quote: NormalTradeQuote,
    side: NormalTradeSide,
    order_type: NormalOrderType,
    limit_price: Decimal | None,
) -> Decimal:
    if order_type is NormalOrderType.NORMAL:
        if limit_price is None:
            raise ExecutionValidationError(
                "limit_price is required for NORMAL orders."
            )
        return limit_price

    candidates: list[Decimal | None] = []
    if side is NormalTradeSide.BUY:
        candidates = [quote.best_ask, quote.last_price, quote.best_bid]
    else:
        candidates = [quote.best_bid, quote.last_price, quote.best_ask]
    for candidate in candidates:
        if candidate is not None and candidate > 0:
            return candidate
    raise ExecutionValidationError(
        "Unable to estimate market-order risk price from quote/order book."
    )


def _extract_order_id(payload: Any) -> str | None:
    if isinstance(payload, list) and payload:
        value = payload[0].get("order_id")
        return str(value) if value not in {None, ""} else None
    if isinstance(payload, dict):
        value = payload.get("order_id")
        return str(value) if value not in {None, ""} else None
    return None


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


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
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
