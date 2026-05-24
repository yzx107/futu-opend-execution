"""Minimal intraday futures VWAP mean-reversion strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from futu_opend_execution._compat import StrEnum
from futu_opend_execution.contracts import ContractSpec
from futu_opend_execution.data.market import MarketState
from futu_opend_execution.ledger.futures import FuturesAction


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


class FuturesSignalStatus(StrEnum):
    DRY_RUN_SIGNAL = "DRY_RUN_SIGNAL"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    RISK_BLOCKED = "RISK_BLOCKED"


class FuturesStrategyAction(StrEnum):
    WAIT = "WAIT"
    BLOCK = "BLOCK"
    BUY_OPEN = "BUY_OPEN"
    SELL_OPEN = "SELL_OPEN"
    SELL_CLOSE = "SELL_CLOSE"
    BUY_CLOSE = "BUY_CLOSE"


@dataclass(frozen=True, slots=True)
class FuturesRiskSnapshot:
    open_long: int = 0
    open_short: int = 0
    realized_net_pnl: Decimal | str | int | float = Decimal("0")
    margin_used: Decimal | str | int | float = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "realized_net_pnl", _decimal(self.realized_net_pnl))
        object.__setattr__(self, "margin_used", _decimal(self.margin_used))


@dataclass(frozen=True, slots=True)
class FuturesMeanReversionRules:
    quantity: int = 1
    entry_vol_multiple: Decimal | str | int | float = Decimal("1.0")
    exit_vol_band: Decimal | str | int | float = Decimal("0.2")
    max_spread_bps: Decimal | str | int | float = Decimal("10")
    min_ticks_to_activate: int = 5
    max_contracts: int = 1
    max_daily_loss: Decimal | str | int | float = Decimal("0")
    max_margin_used: Decimal | str | int | float = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_vol_multiple", _decimal(self.entry_vol_multiple))
        object.__setattr__(self, "exit_vol_band", _decimal(self.exit_vol_band))
        object.__setattr__(self, "max_spread_bps", _decimal(self.max_spread_bps))
        object.__setattr__(self, "max_daily_loss", _decimal(self.max_daily_loss))
        object.__setattr__(self, "max_margin_used", _decimal(self.max_margin_used))
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.entry_vol_multiple <= 0:
            raise ValueError("entry_vol_multiple must be positive")
        if self.exit_vol_band < 0:
            raise ValueError("exit_vol_band must be non-negative")
        if self.max_spread_bps < 0:
            raise ValueError("max_spread_bps must be non-negative")
        if self.min_ticks_to_activate < 0:
            raise ValueError("min_ticks_to_activate must be non-negative")
        if self.max_contracts <= 0:
            raise ValueError("max_contracts must be positive")
        if self.max_daily_loss < 0:
            raise ValueError("max_daily_loss must be non-negative")
        if self.max_margin_used < 0:
            raise ValueError("max_margin_used must be non-negative")

    def to_jsonable(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = _str_decimal(value)
        return payload


@dataclass(frozen=True, slots=True)
class FuturesSignal:
    symbol: str
    timestamp: datetime
    action: FuturesStrategyAction
    quantity: int
    limit_price: Decimal | None
    status: FuturesSignalStatus
    reason: str
    reference_vwap: Decimal | None
    realized_vol: Decimal
    spread_bps: Decimal

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action.value,
            "quantity": self.quantity,
            "limit_price": _str_decimal(self.limit_price) if self.limit_price is not None else None,
            "status": self.status.value,
            "reason": self.reason,
            "reference_vwap": _str_decimal(self.reference_vwap) if self.reference_vwap is not None else None,
            "realized_vol": _str_decimal(self.realized_vol),
            "spread_bps": _str_decimal(self.spread_bps),
        }


class FuturesMeanReversionStrategy:
    def __init__(self, *, contract: ContractSpec, rules: FuturesMeanReversionRules = FuturesMeanReversionRules()) -> None:
        self.contract = contract
        self.rules = rules

    def evaluate(self, *, market: MarketState, risk: FuturesRiskSnapshot) -> FuturesSignal:
        blocked = self._risk_block(market, risk)
        if blocked is not None:
            return self._signal(market, FuturesStrategyAction.BLOCK, 0, None, FuturesSignalStatus.RISK_BLOCKED, blocked)

        reference = market.rolling_vwap or market.opening_vwap
        if reference is None or market.last_price is None or market.realized_vol <= 0:
            return self._signal(market, FuturesStrategyAction.WAIT, 0, None, FuturesSignalStatus.NOT_EXECUTABLE, "anchor/vol unavailable")
        if market.tick_count < self.rules.min_ticks_to_activate:
            return self._signal(market, FuturesStrategyAction.WAIT, 0, None, FuturesSignalStatus.NOT_EXECUTABLE, "activation threshold not met")

        long_qty = risk.open_long
        short_qty = risk.open_short
        if long_qty > 0 and market.last_price >= reference - self.rules.exit_vol_band * market.realized_vol:
            return self._signal(market, FuturesStrategyAction.SELL_CLOSE, min(long_qty, self.rules.quantity), market.best_bid, FuturesSignalStatus.DRY_RUN_SIGNAL, "long reverted near vwap")
        if short_qty > 0 and market.last_price <= reference + self.rules.exit_vol_band * market.realized_vol:
            return self._signal(market, FuturesStrategyAction.BUY_CLOSE, min(short_qty, self.rules.quantity), market.best_ask, FuturesSignalStatus.DRY_RUN_SIGNAL, "short reverted near vwap")

        total_open = long_qty + short_qty
        if total_open >= self.rules.max_contracts:
            return self._signal(market, FuturesStrategyAction.WAIT, 0, None, FuturesSignalStatus.NOT_EXECUTABLE, "max contracts reached")

        threshold = self.rules.entry_vol_multiple * market.realized_vol
        if market.last_price <= reference - threshold:
            margin_block = self._margin_block(market.best_ask, risk)
            if margin_block is not None:
                return self._signal(market, FuturesStrategyAction.BLOCK, 0, None, FuturesSignalStatus.RISK_BLOCKED, margin_block)
            return self._signal(market, FuturesStrategyAction.BUY_OPEN, self.rules.quantity, market.best_ask, FuturesSignalStatus.DRY_RUN_SIGNAL, "below vwap deviation")
        if market.last_price >= reference + threshold:
            margin_block = self._margin_block(market.best_bid, risk)
            if margin_block is not None:
                return self._signal(market, FuturesStrategyAction.BLOCK, 0, None, FuturesSignalStatus.RISK_BLOCKED, margin_block)
            return self._signal(market, FuturesStrategyAction.SELL_OPEN, self.rules.quantity, market.best_bid, FuturesSignalStatus.DRY_RUN_SIGNAL, "above vwap deviation")
        return self._signal(market, FuturesStrategyAction.WAIT, 0, None, FuturesSignalStatus.NOT_EXECUTABLE, "inside no-trade band")

    def _risk_block(self, market: MarketState, risk: FuturesRiskSnapshot) -> str | None:
        if market.symbol != self.contract.symbol:
            return "symbol mismatch"
        if market.stale:
            return "market data stale"
        if market.orderbook_limited or market.best_bid is None or market.best_ask is None:
            return "book depth unavailable"
        if market.spread_bps > self.rules.max_spread_bps:
            return "spread too wide"
        if self.rules.max_daily_loss > 0 and risk.realized_net_pnl <= -self.rules.max_daily_loss:
            return "max daily loss reached"
        if self.rules.max_margin_used > 0 and risk.margin_used >= self.rules.max_margin_used:
            return "max margin reached"
        if market.best_bid <= 0 or market.best_ask <= 0:
            return "invalid quote"
        return None

    def _margin_block(self, price: Decimal | None, risk: FuturesRiskSnapshot) -> str | None:
        if self.rules.max_margin_used <= 0 or price is None:
            return None
        proposed = risk.margin_used + self.contract.initial_margin(price=price, quantity=self.rules.quantity)
        if proposed > self.rules.max_margin_used:
            return "proposed margin exceeds max"
        return None

    def _signal(
        self,
        market: MarketState,
        action: FuturesStrategyAction,
        quantity: int,
        limit_price: Decimal | None,
        status: FuturesSignalStatus,
        reason: str,
    ) -> FuturesSignal:
        return FuturesSignal(
            symbol=self.contract.symbol,
            timestamp=market.timestamp,
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            status=status if limit_price is not None or action in {FuturesStrategyAction.WAIT, FuturesStrategyAction.BLOCK} else FuturesSignalStatus.RISK_BLOCKED,
            reason=reason if limit_price is not None or action in {FuturesStrategyAction.WAIT, FuturesStrategyAction.BLOCK} else "limit price unavailable",
            reference_vwap=market.rolling_vwap or market.opening_vwap,
            realized_vol=market.realized_vol,
            spread_bps=market.spread_bps,
        )


def to_ledger_action(action: FuturesStrategyAction) -> FuturesAction | None:
    if action is FuturesStrategyAction.BUY_OPEN:
        return FuturesAction.BUY_OPEN
    if action is FuturesStrategyAction.SELL_OPEN:
        return FuturesAction.SELL_OPEN
    if action is FuturesStrategyAction.SELL_CLOSE:
        return FuturesAction.SELL_CLOSE
    if action is FuturesStrategyAction.BUY_CLOSE:
        return FuturesAction.BUY_CLOSE
    return None
