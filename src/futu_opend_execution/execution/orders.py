"""Generic real-order intent models for the OpenD trading agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from futu_opend_execution._compat import StrEnum, UTC


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderRole(StrEnum):
    CORE_BUY = "CORE_BUY"
    TRADING_BUY = "TRADING_BUY"
    TRADING_SELL = "TRADING_SELL"
    TRADING_REBUY = "TRADING_REBUY"


class OrderSource(StrEnum):
    STRATEGY = "STRATEGY"
    MANUAL = "MANUAL"
    RECONCILIATION = "RECONCILIATION"


@dataclass(frozen=True, slots=True)
class RealOrderIntent:
    symbol: str
    side: OrderSide | str
    quantity: int
    limit_price: Decimal | str | int | float
    role: OrderRole | str
    source: OrderSource | str = OrderSource.STRATEGY
    remark: str = "opend_trading_agent"
    client_intent_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if "." not in symbol:
            symbol = f"HK.{symbol}"
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", self.side if isinstance(self.side, OrderSide) else OrderSide(str(self.side).upper()))
        object.__setattr__(self, "role", self.role if isinstance(self.role, OrderRole) else OrderRole(str(self.role).upper()))
        object.__setattr__(self, "source", self.source if isinstance(self.source, OrderSource) else OrderSource(str(self.source).upper()))
        object.__setattr__(self, "limit_price", self.limit_price if isinstance(self.limit_price, Decimal) else Decimal(str(self.limit_price)))
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.side is OrderSide.SELL and self.role is not OrderRole.TRADING_SELL:
            raise ValueError("SELL is allowed only for trading inventory")
        if self.side is OrderSide.BUY and self.role is OrderRole.TRADING_SELL:
            raise ValueError("TRADING_SELL must use SELL side")

    @property
    def notional(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)

