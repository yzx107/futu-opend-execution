"""Small shared execution models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from futu_opend_execution._compat import StrEnum


class MarketSession(StrEnum):
    HK = "HK"


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


@dataclass(frozen=True, slots=True)
class QuoteLevel:
    price: Decimal | str | int | float
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price))


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    symbol: str
    asks: tuple[QuoteLevel, ...]
    bids: tuple[QuoteLevel, ...] = ()
    observed_at: datetime | None = None
    session: MarketSession = MarketSession.HK


@dataclass(frozen=True, slots=True)
class FillLeg:
    price: Decimal | str | int | float
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price))


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
class BrokerOrderSnapshot:
    order_id: str
    symbol: str
    status: BrokerOrderStatus | str
    quantity: int
    price: Decimal | str | int | float
    dealt_quantity: int = 0
    dealt_avg_price: Decimal | str | int | float | None = None
    updated_time: str | None = None
    message: str = ""
    raw_payload: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", str(self.order_id))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "status", _status(self.status))
        object.__setattr__(self, "price", _decimal(self.price))
        object.__setattr__(self, "dealt_avg_price", None if self.dealt_avg_price is None else _decimal(self.dealt_avg_price))
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


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _status(value: BrokerOrderStatus | str) -> BrokerOrderStatus:
    if isinstance(value, BrokerOrderStatus):
        return value
    try:
        return BrokerOrderStatus(str(value).strip().upper())
    except ValueError:
        return BrokerOrderStatus.UNKNOWN
