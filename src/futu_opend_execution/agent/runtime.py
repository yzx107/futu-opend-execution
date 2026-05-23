"""Runtime loops for replay, paper, live dry-run, and guarded auto-real."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import sleep as _sleep
from typing import Iterable

from futu_opend_execution.agent.risk import RealOrderGuard
from futu_opend_execution.data.market import MarketState, market_state_to_jsonable
from futu_opend_execution.execution.orders import OrderRole, OrderSide, OrderSource, RealOrderIntent
from futu_opend_execution.inventory import InventoryState
from futu_opend_execution.ledger.paper import PaperLedger, summarize_paper_ledger
from futu_opend_execution.models import BrokerOrderSnapshot, TimeInForce, TradeMode
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerExecutableIntent,
    CostReducerExecutableStatus,
    CostReducerExecutionPolicy,
    CostReducerRules,
    CostReducerState,
    apply_dry_run_fill,
)
from futu_opend_execution.strategies.cost_reducer import CostReducerStrategy
from futu_opend_execution.strategy_config import ExecutionMode


@dataclass(frozen=True, slots=True)
class TradingAgentConfig:
    symbol: str
    current_qty: int
    cost_price: Decimal | str | int | float
    lot_size: int
    max_sell_qty_per_order: int | None = None
    max_rebuy_qty_per_order: int | None = None
    max_round_trips: int = 1

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if "." not in symbol:
            symbol = f"HK.{symbol}"
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "cost_price", _decimal(self.cost_price))
        if self.current_qty <= 0:
            raise ValueError("current_qty must be positive")
        if self.lot_size <= 0 or self.current_qty % self.lot_size != 0:
            raise ValueError("current_qty must be lot-aligned")


def build_inventory_for_existing_position(config: TradingAgentConfig) -> InventoryState:
    lots = config.current_qty // config.lot_size
    trading_lots = max(lots // 2, 1)
    core_lots = lots - trading_lots
    if core_lots <= 0:
        raise ValueError("position must have at least one core and one trading lot")
    inventory = InventoryState(
        core_qty_target=core_lots * config.lot_size,
        trading_qty_target=trading_lots * config.lot_size,
    )
    inventory.seed_opening_inventory(anchor_price=config.cost_price)
    return inventory


def default_strategy(config: TradingAgentConfig) -> CostReducerStrategy:
    rules = CostReducerRules(
        max_round_trips=config.max_round_trips,
        max_sell_total_position_ratio=Decimal(str((config.max_sell_qty_per_order or config.lot_size) / config.current_qty)),
    )
    policy = CostReducerExecutionPolicy(
        dry_run_only=True,
        manual_approval_required=True,
        enable_real_sell=False,
        enable_real_rebuy=False,
        max_real_sell_qty=0,
        max_real_rebuy_qty=0,
        tick_size=Decimal("0.01"),
    )
    return CostReducerStrategy(rules=rules, policy=policy)


def run_replay(
    *,
    config: TradingAgentConfig,
    market_states: Iterable[MarketState],
    log_path: Path | str,
    apply_paper_fills: bool = True,
) -> dict[str, object]:
    inventory = build_inventory_for_existing_position(config)
    state = CostReducerState()
    strategy = default_strategy(config)
    total_sell_intents = 0
    total_rebuy_intents = 0
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    for market in market_states:
        intent = strategy.evaluate(market=market, inventory=inventory, state=state)
        if intent.action is CostReducerAction.SELL_TRADING:
            total_sell_intents += 1
        if intent.action is CostReducerAction.REBUY_TRADING:
            total_rebuy_intents += 1
        _write_jsonl(log, {"event": "market_state", **market_state_to_jsonable(market)})
        _write_jsonl(log, {"event": "strategy_signal", "symbol": config.symbol, **intent_to_jsonable(intent)})
        if apply_paper_fills and intent.status is CostReducerExecutableStatus.BLOCKED and intent.limit_price is not None:
            apply_dry_run_fill(
                decision=_decision_from_intent(intent),
                market=_adaptive_like(market),
                inventory=inventory,
                state=state,
                estimated_roundtrip_cost_bps=strategy.rules.estimated_roundtrip_cost_bps,
            )
    summary = {
        "event": "replay_summary",
        "total_sell_intents": total_sell_intents,
        "total_rebuy_intents": total_rebuy_intents,
        "final_current_position": inventory.current_position,
        "final_economic_cost_basis": str(inventory.economic_cost_basis),
        "final_trading_qty_sold": inventory.trading_qty_sold,
        "final_trading_qty_rebought": inventory.trading_qty_rebought,
        "round_trips_completed": state.round_trips_completed,
        "last_sell_price": str(state.last_sell_price) if state.last_sell_price is not None else None,
    }
    _write_jsonl(log, summary)
    return summary


def run_paper(*, replay_log_path: Path | str, ledger_path: Path | str, report_path: Path | str | None = None) -> dict[str, object]:
    ledger = PaperLedger(ledger_path)
    for row in _read_jsonl(replay_log_path):
        if row.get("event") != "strategy_signal":
            continue
        ledger.record_trade(
            symbol=str(row.get("symbol", "")),
            action=str(row.get("action", "")),
            quantity=int(row.get("quantity", 0) or 0),
            price=row.get("limit_price") or 0,
            reason=str(row.get("reason", "")),
            event_id=str(row.get("client_intent_id") or ""),
        )
    summary = summarize_paper_ledger(ledger_path)
    if report_path is not None:
        report = Path(report_path)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run_monitor(
    *,
    config: TradingAgentConfig,
    provider,
    log_path: Path | str,
    iterations: int = 1,
    interval_seconds: float = 1.0,
    sleep=_sleep,
) -> list[dict[str, object]]:
    inventory = build_inventory_for_existing_position(config)
    state = CostReducerState()
    strategy = default_strategy(config)
    events: list[dict[str, object]] = []
    for index in range(iterations):
        market = provider.read_once()
        intent = strategy.evaluate(market=market, inventory=inventory, state=state)
        payload = {"event": "strategy_signal", "symbol": config.symbol, **intent_to_jsonable(intent)}
        _write_jsonl(log_path, {"event": "market_state", **market_state_to_jsonable(market)})
        _write_jsonl(log_path, payload)
        events.append(payload)
        if index < iterations - 1:
            sleep(max(interval_seconds, 0.0))
    return events


def submit_auto_real_intent(
    *,
    intent: CostReducerExecutableIntent,
    symbol: str,
    broker,
    guard: RealOrderGuard,
    inventory: InventoryState,
    market_snapshot: dict[str, object],
    confirm_text: str,
    enable_auto_cost_reducer: bool = False,
    now_monotonic: float = 0.0,
) -> BrokerOrderSnapshot:
    real_intent = real_order_intent_from_signal(intent, symbol=symbol)
    guard.validate(
        real_intent,
        execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_AUTO,
        inventory=inventory,
        market_snapshot=market_snapshot,
        confirm_text=confirm_text,
        enable_auto_cost_reducer=enable_auto_cost_reducer,
        approved=True,
        now_monotonic=now_monotonic,
    )
    if real_intent.side is OrderSide.SELL:
        return broker.place_limit_sell(
            symbol=real_intent.symbol,
            quantity=real_intent.quantity,
            limit_price=real_intent.limit_price,
            trade_mode=TradeMode.REAL,
            time_in_force=TimeInForce.DAY,
            remark=real_intent.remark,
        )
    return broker.place_limit_buy(
        symbol=real_intent.symbol,
        quantity=real_intent.quantity,
        limit_price=real_intent.limit_price,
        trade_mode=TradeMode.REAL,
        time_in_force=TimeInForce.DAY,
        remark=real_intent.remark,
    )


def real_order_intent_from_signal(intent: CostReducerExecutableIntent, *, symbol: str) -> RealOrderIntent:
    if intent.limit_price is None or intent.quantity <= 0 or intent.side is None or intent.role is None:
        raise ValueError("strategy signal is not executable")
    return RealOrderIntent(
        symbol=symbol,
        side=intent.side,
        quantity=intent.quantity,
        limit_price=intent.limit_price,
        role=intent.role,
        source=OrderSource.STRATEGY,
        remark="cost_reducer",
    )


def intent_to_jsonable(intent: CostReducerExecutableIntent) -> dict[str, object]:
    def encode(value):
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        return value

    return {
        "action": intent.action.value,
        "side": encode(intent.side),
        "role": encode(intent.role),
        "quantity": intent.quantity,
        "limit_price": encode(intent.limit_price),
        "reason": intent.reason,
        "expected_edge_bps": encode(intent.expected_edge_bps),
        "status": intent.status.value,
        "market_snapshot": encode(intent.market_snapshot),
        "inventory_snapshot": encode(intent.inventory_snapshot),
    }


def _decision_from_intent(intent: CostReducerExecutableIntent):
    from futu_opend_execution.services.cost_reducer import CostReducerDecision

    return CostReducerDecision(intent.action, quantity=intent.quantity, reason=intent.reason)


def _adaptive_like(market: MarketState):
    from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState

    return AdaptiveMarketState(
        opening_vwap=market.opening_vwap,
        rolling_vwap=market.rolling_vwap,
        realized_vol=market.realized_vol,
        rolling_high=market.rolling_high,
        rolling_low=market.rolling_low,
        cumulative_turnover=market.cumulative_turnover,
        volume_delta=market.volume_delta,
        turnover_delta=market.turnover_delta,
        cumulative_field_reset_detected=False,
        tick_count=market.tick_count,
        orderbook_imbalance=market.orderbook_imbalance,
        spread_bps=market.spread_bps,
        last_price=market.last_price,
    )


def _write_jsonl(path: Path | str, row: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path | str) -> list[dict[str, object]]:
    target = Path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
