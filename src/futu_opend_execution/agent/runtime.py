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
    CostReducerDecision,
    CostReducerExecutableIntent,
    CostReducerExecutableStatus,
    CostReducerExecutionPolicy,
    CostReducerRules,
    CostReducerState,
    apply_dry_run_fill,
    build_executable_intent,
)
from futu_opend_execution.strategies.cost_reducer import CostReducerStrategy
from futu_opend_execution.strategy_config import ExecutionMode
from futu_opend_execution.risk_sentinel import BlackSwanSentinel, risk_events_to_jsonl, should_pause_trading
from futu_opend_execution.watchlist import CostReducerSymbolRules, WatchSymbolConfig, WatchlistConfig


@dataclass(frozen=True, slots=True)
class TradingAgentConfig:
    symbol: str
    current_qty: int
    cost_price: Decimal | str | int | float
    lot_size: int
    core_qty_target: int | None = None
    trading_qty_target: int | None = None
    max_sell_qty_per_order: int | None = None
    max_rebuy_qty_per_order: int | None = None
    max_sell_total_position_ratio: Decimal | str | int | float = Decimal("0.5")
    max_round_trips: int = 1
    cost_reducer_rules: CostReducerSymbolRules | None = None

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
        if self.core_qty_target is not None and self.trading_qty_target is not None:
            if self.core_qty_target + self.trading_qty_target != self.current_qty:
                raise ValueError("core_qty_target + trading_qty_target must equal current_qty")
            if self.core_qty_target % self.lot_size != 0 or self.trading_qty_target % self.lot_size != 0:
                raise ValueError("core/trading targets must be lot-aligned")
        object.__setattr__(self, "max_sell_total_position_ratio", _decimal(self.max_sell_total_position_ratio))

    @classmethod
    def from_watch_symbol(cls, item: WatchSymbolConfig) -> "TradingAgentConfig":
        return cls(
            symbol=item.symbol,
            current_qty=item.current_qty,
            cost_price=item.cost_price,
            lot_size=item.lot_size,
            core_qty_target=item.core_qty_target,
            trading_qty_target=item.trading_qty_target,
            max_sell_qty_per_order=item.max_sell_qty_per_order,
            max_rebuy_qty_per_order=item.max_rebuy_qty_per_order,
            max_sell_total_position_ratio=item.max_sell_total_position_ratio,
            max_round_trips=item.max_round_trips,
            cost_reducer_rules=item.cost_reducer_rules,
        )


def build_inventory_for_existing_position(config: TradingAgentConfig) -> InventoryState:
    if config.core_qty_target is not None and config.trading_qty_target is not None:
        inventory = InventoryState(
            core_qty_target=config.core_qty_target,
            trading_qty_target=config.trading_qty_target,
        )
        inventory.seed_opening_inventory(anchor_price=config.cost_price)
        return inventory
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
    symbol_rules = config.cost_reducer_rules
    rules = CostReducerRules(
        lot_size=config.lot_size,
        max_round_trips=config.max_round_trips,
        max_sell_total_position_ratio=_decimal(config.max_sell_total_position_ratio),
        max_sell_qty_per_order=config.max_sell_qty_per_order,
        max_rebuy_qty_per_order=config.max_rebuy_qty_per_order,
        overextension_vol_multiple=symbol_rules.overextension_vol_multiple if symbol_rules else Decimal("2.0"),
        high_pullback_vol_multiple=symbol_rules.high_pullback_vol_multiple if symbol_rules else Decimal("0.5"),
        rebuy_anchor_vol_band=symbol_rules.rebuy_anchor_vol_band if symbol_rules else Decimal("1.0"),
        max_spread_bps=symbol_rules.max_spread_bps if symbol_rules else Decimal("20"),
        estimated_roundtrip_cost_bps=symbol_rules.estimated_roundtrip_cost_bps if symbol_rules else Decimal("35"),
        safety_buffer_bps=symbol_rules.safety_buffer_bps if symbol_rules else Decimal("20"),
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
        _write_jsonl(log, _market_state_row(market, mode="replay"))
        _write_jsonl(log, _strategy_signal_row(config.symbol, market, intent, mode="replay"))
        if apply_paper_fills and intent.status is CostReducerExecutableStatus.DRY_RUN_SIGNAL and intent.limit_price is not None:
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
        if row.get("status") != CostReducerExecutableStatus.DRY_RUN_SIGNAL.value:
            continue
        ledger.record_trade(
            symbol=str(row.get("symbol", "")),
            action=str(row.get("action", "")),
            quantity=int(row.get("quantity", 0) or 0),
            price=row.get("limit_price") or 0,
            timestamp=str(row.get("timestamp") or ""),
            reason=str(row.get("reason", "")),
            event_id=str(row.get("client_intent_id") or row.get("signal_id") or ""),
            status=str(row.get("status") or ""),
            expected_edge_bps=row.get("expected_edge_bps"),
            estimated_cost_bps=row.get("estimated_cost_bps"),
            cost_basis_before=(row.get("inventory_snapshot") or {}).get("economic_cost_basis") if isinstance(row.get("inventory_snapshot"), dict) else None,
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
    sentinel = BlackSwanSentinel()
    events: list[dict[str, object]] = []
    for index in range(iterations):
        try:
            market = provider.read_once(config.symbol)
        except TypeError:
            market = provider.read_once()
        except Exception as exc:
            risk_event = sentinel.provider_error(symbol=config.symbol, message=str(exc))
            risk_events_to_jsonl([risk_event], monitor_log_path=log_path)
            events.append(risk_event.to_jsonable())
            if index < iterations - 1:
                sleep(max(interval_seconds, 0.0))
            continue
        intent = strategy.evaluate(market=market, inventory=inventory, state=state)
        payload = _strategy_signal_row(config.symbol, market, intent, mode="live-dry-run")
        _write_jsonl(log_path, _market_state_row(market, mode="live-dry-run"))
        _write_jsonl(log_path, payload)
        events.append(payload)
        if index < iterations - 1:
            sleep(max(interval_seconds, 0.0))
    return events


def run_watchlist_monitor(
    *,
    watchlist: WatchlistConfig,
    provider,
    log_path: Path | str,
    iterations: int = 1,
    interval_seconds: float = 1.0,
    mode: str = "live-dry-run",
    sleep=_sleep,
) -> list[dict[str, object]]:
    configs = {item.symbol: TradingAgentConfig.from_watch_symbol(item) for item in watchlist.enabled_symbols}
    inventories = {symbol: build_inventory_for_existing_position(config) for symbol, config in configs.items()}
    states = {symbol: CostReducerState() for symbol in configs}
    strategies = {symbol: default_strategy(config) for symbol, config in configs.items()}
    watch_items = {item.symbol: item for item in watchlist.enabled_symbols}
    sentinel = BlackSwanSentinel()
    rows: list[dict[str, object]] = []
    for index in range(iterations):
        for symbol, config in configs.items():
            try:
                market = provider.read_once(symbol)
            except TypeError:
                market = provider.read_once()
            except Exception as exc:
                risk_event = sentinel.provider_error(symbol=symbol, message=str(exc))
                risk_events_to_jsonl([risk_event], monitor_log_path=log_path, mode=mode)
                row = risk_event.to_jsonable(mode=mode)
                rows.append(row)
                continue

            market_row = _market_state_row(market, mode=mode)
            _write_jsonl(log_path, market_row)
            rows.append(market_row)

            risk_now = market.timestamp if market.source == "fake_live" else None
            risk_events = sentinel.evaluate(market=market, config=watch_items[symbol], now=risk_now)
            risk_events_to_jsonl(risk_events, monitor_log_path=log_path, mode=mode)
            rows.extend(event.to_jsonable(mode=mode) for event in risk_events)

            if should_pause_trading(risk_events):
                intent = _risk_blocked_intent(
                    strategy=strategies[symbol],
                    market=market,
                    inventory=inventories[symbol],
                    state=states[symbol],
                    reason="risk sentinel paused trading",
                )
            else:
                intent = strategies[symbol].evaluate(
                    market=market,
                    inventory=inventories[symbol],
                    state=states[symbol],
                )
            signal_row = _strategy_signal_row(config.symbol, market, intent, mode=mode)
            _write_jsonl(log_path, signal_row)
            rows.append(signal_row)
        if index < iterations - 1:
            sleep(max(interval_seconds, 0.0))
    return rows


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
        "estimated_cost_bps": encode(intent.estimated_cost_bps),
        "safety_buffer_bps": encode(intent.safety_buffer_bps),
        "status": intent.status.value,
        "client_intent_id": intent.client_intent_id,
        "signal_id": intent.signal_id,
        "market_snapshot": encode(intent.market_snapshot),
        "inventory_snapshot": encode(intent.inventory_snapshot),
    }


def _risk_blocked_intent(
    *,
    strategy: CostReducerStrategy,
    market: MarketState,
    inventory: InventoryState,
    state: CostReducerState,
    reason: str,
) -> CostReducerExecutableIntent:
    return build_executable_intent(
        decision=CostReducerDecision(CostReducerAction.BLOCK, reason=reason),
        market=_adaptive_like(market),
        inventory=inventory,
        rules=strategy.rules,
        policy=strategy.policy,
        best_bid=market.best_bid,
        best_ask=market.best_ask,
        last_sell_price=state.last_sell_price,
    )


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


def _market_state_row(market: MarketState, *, mode: str) -> dict[str, object]:
    payload = market_state_to_jsonable(market)
    return {
        **payload,
        "event": "market_state",
        "symbol": market.symbol,
        "timestamp": market.timestamp.isoformat(),
        "mode": mode,
        "source": market.source,
        "payload": payload,
    }


def _strategy_signal_row(symbol: str, market: MarketState, intent: CostReducerExecutableIntent, *, mode: str) -> dict[str, object]:
    payload = intent_to_jsonable(intent)
    return {
        "event": "strategy_signal",
        "symbol": symbol,
        "timestamp": market.timestamp.isoformat(),
        "mode": mode,
        "source": "cost_reducer",
        **payload,
        "payload": payload,
    }


def _read_jsonl(path: Path | str) -> list[dict[str, object]]:
    target = Path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
