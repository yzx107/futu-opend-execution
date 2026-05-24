"""Runtime loop for futures replay and paper strategy validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from futu_opend_execution.contracts import ContractSpec
from futu_opend_execution.data.market import MarketState, market_state_to_jsonable
from futu_opend_execution.ledger.futures import FuturesPaperLedger, summarize_futures_paper_ledger
from futu_opend_execution.strategies.futures_mean_reversion import (
    FuturesMeanReversionRules,
    FuturesMeanReversionStrategy,
    FuturesRiskSnapshot,
    FuturesSignalStatus,
    to_ledger_action,
)


@dataclass(frozen=True, slots=True)
class FuturesReplayConfig:
    contract: ContractSpec
    rules: FuturesMeanReversionRules = FuturesMeanReversionRules()
    apply_paper_fills: bool = True


def run_futures_replay(
    *,
    config: FuturesReplayConfig,
    market_states: Iterable[MarketState],
    log_path: Path | str,
    ledger_path: Path | str,
) -> dict[str, object]:
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    ledger = FuturesPaperLedger(ledger_path, contracts={config.contract.symbol: config.contract})
    strategy = FuturesMeanReversionStrategy(contract=config.contract, rules=config.rules)
    total_signals = 0
    total_blocks = 0
    last_price: Decimal | None = None

    for market in market_states:
        if market.last_price is not None:
            last_price = market.last_price
        risk = _risk_snapshot(ledger=ledger, contract=config.contract, last_price=last_price)
        signal = strategy.evaluate(market=market, risk=risk)
        if signal.status is FuturesSignalStatus.DRY_RUN_SIGNAL:
            total_signals += 1
        if signal.status is FuturesSignalStatus.RISK_BLOCKED:
            total_blocks += 1
        _write_jsonl(log, {"event": "futures_market_state", **market_state_to_jsonable(market)})
        signal_row = {
            "event": "futures_strategy_signal",
            "signal_id": _signal_id(signal.to_jsonable()),
            **signal.to_jsonable(),
            "rules": config.rules.to_jsonable(),
        }
        _write_jsonl(log, signal_row)
        ledger_action = to_ledger_action(signal.action)
        if (
            config.apply_paper_fills
            and ledger_action is not None
            and signal.status is FuturesSignalStatus.DRY_RUN_SIGNAL
            and signal.limit_price is not None
        ):
            ledger.record_fill(
                symbol=config.contract.symbol,
                action=ledger_action,
                quantity=signal.quantity,
                price=signal.limit_price,
                timestamp=signal.timestamp.isoformat(),
                event_id=str(signal_row["signal_id"]),
                reason=signal.reason,
            )

    marks = {config.contract.symbol: last_price} if last_price is not None else None
    summary = summarize_futures_paper_ledger(ledger_path, contracts={config.contract.symbol: config.contract}, mark_prices=marks)
    summary.update(
        {
            "event": "futures_replay_summary",
            "symbol": config.contract.symbol,
            "total_signals": total_signals,
            "total_blocks": total_blocks,
            "last_price": str(last_price) if last_price is not None else None,
        }
    )
    _write_jsonl(log, summary)
    return summary


def _risk_snapshot(*, ledger: FuturesPaperLedger, contract: ContractSpec, last_price: Decimal | None) -> FuturesRiskSnapshot:
    positions = ledger.open_positions.get(contract.symbol, {})
    marks = {contract.symbol: last_price} if last_price is not None else None
    summary = ledger.summary(mark_prices=marks)
    return FuturesRiskSnapshot(
        open_long=int(positions.get("LONG", 0)),
        open_short=int(positions.get("SHORT", 0)),
        realized_net_pnl=summary["realized_net_pnl"],
        margin_used=summary["margin_used"],
    )


def _signal_id(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _write_jsonl(path: Path | str, row: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
