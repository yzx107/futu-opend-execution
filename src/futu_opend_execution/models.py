"""Typed execution models used by the planner and simulator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


def _to_decimal(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class MarketSession(StrEnum):
    GREY = "GREY"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"


class TradeMode(StrEnum):
    SIMULATED = "SIMULATED"
    REAL = "REAL"


class BrokerOrderStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    WAITING_SUBMIT = "WAITING_SUBMIT"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    FILLED_PART = "FILLED_PART"
    FILLED_ALL = "FILLED_ALL"
    CANCELLED_PART = "CANCELLED_PART"
    CANCELLED_ALL = "CANCELLED_ALL"
    FAILED = "FAILED"
    DISABLED = "DISABLED"
    DELETED = "DELETED"
    FILL_CANCELLED = "FILL_CANCELLED"


def _to_broker_order_status(value: BrokerOrderStatus | str) -> BrokerOrderStatus:
    if isinstance(value, BrokerOrderStatus):
        return value
    try:
        return BrokerOrderStatus(str(value).strip().upper())
    except ValueError:
        return BrokerOrderStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class QuoteLevel:
    price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _to_decimal(self.price))


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    symbol: str
    asks: tuple[QuoteLevel, ...]
    bids: tuple[QuoteLevel, ...] = ()
    observed_at: datetime | None = None
    session: MarketSession = MarketSession.GREY

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "asks", tuple(self.asks))
        object.__setattr__(self, "bids", tuple(self.bids))


@dataclass(frozen=True, slots=True)
class GreyMarketBuyRequest:
    symbol: str
    quantity: int
    trade_mode: TradeMode = TradeMode.SIMULATED
    time_in_force: TimeInForce = TimeInForce.IOC
    tick_size: Decimal = Decimal("0.001")
    price_buffer_ticks: int = 0
    max_limit_price: Decimal | None = None
    allow_partial_fill: bool = False
    ioc_timeout_seconds: Decimal | None = Decimal("1.0")
    remark: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "tick_size", _to_decimal(self.tick_size))
        object.__setattr__(
            self,
            "max_limit_price",
            _to_decimal(self.max_limit_price),
        )
        object.__setattr__(
            self,
            "ioc_timeout_seconds",
            _to_decimal(self.ioc_timeout_seconds),
        )


@dataclass(frozen=True, slots=True)
class FillLeg:
    price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _to_decimal(self.price))


@dataclass(frozen=True, slots=True)
class SimulatedExecutionResult:
    requested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    average_price: Decimal | None
    total_notional: Decimal
    fills: tuple[FillLeg, ...]

    @property
    def fully_filled(self) -> bool:
        return self.remaining_quantity == 0


@dataclass(frozen=True, slots=True)
class GreyMarketBuyPlan:
    symbol: str
    quantity: int
    side: OrderSide
    trade_mode: TradeMode
    session: MarketSession
    time_in_force: TimeInForce
    minimum_limit_price: Decimal
    selected_limit_price: Decimal
    expected_fill: SimulatedExecutionResult
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    order_id: str
    symbol: str
    status: BrokerOrderStatus | str
    quantity: int
    price: Decimal
    dealt_quantity: int = 0
    dealt_avg_price: Decimal | None = None
    updated_time: str | None = None
    message: str = ""
    raw_payload: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", str(self.order_id))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "status", _to_broker_order_status(self.status))
        object.__setattr__(self, "price", _to_decimal(self.price))
        object.__setattr__(
            self,
            "dealt_avg_price",
            _to_decimal(self.dealt_avg_price),
        )
        if self.raw_payload is not None:
            object.__setattr__(self, "raw_payload", dict(self.raw_payload))

    @property
    def terminal(self) -> bool:
        return self.status in {
            BrokerOrderStatus.FILLED_ALL,
            BrokerOrderStatus.CANCELLED_PART,
            BrokerOrderStatus.CANCELLED_ALL,
            BrokerOrderStatus.FAILED,
            BrokerOrderStatus.DISABLED,
            BrokerOrderStatus.DELETED,
            BrokerOrderStatus.FILL_CANCELLED,
        }

    @property
    def fully_filled(self) -> bool:
        return self.status is BrokerOrderStatus.FILLED_ALL

    @property
    def remaining_quantity(self) -> int:
        return max(self.quantity - self.dealt_quantity, 0)


@dataclass(frozen=True, slots=True)
class GreyMarketExecutionReport:
    plan: GreyMarketBuyPlan
    requested_time_in_force: TimeInForce
    submitted_time_in_force: TimeInForce
    initial_order: BrokerOrderSnapshot
    latest_order: BrokerOrderSnapshot
    canceled_for_timeout: bool
    ioc_emulation_used: bool
    timeline: tuple[BrokerOrderSnapshot, ...]

    @property
    def fully_filled(self) -> bool:
        return self.latest_order.fully_filled

    @property
    def remaining_quantity(self) -> int:
        return self.latest_order.remaining_quantity


@dataclass(frozen=True, slots=True)
class GreyMarketSnatchReport:
    market_state: str
    waited_seconds: Decimal
    order_book: OrderBookSnapshot
    plan: GreyMarketBuyPlan
    execution_report: GreyMarketExecutionReport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_state", self.market_state.strip().upper())
        object.__setattr__(self, "waited_seconds", _to_decimal(self.waited_seconds))

    @property
    def submitted(self) -> bool:
        return self.execution_report is not None
