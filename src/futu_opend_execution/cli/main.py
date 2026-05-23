"""CLI for the OpenD Trading Agent."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from futu_opend_execution.agent.runtime import TradingAgentConfig, run_monitor, run_paper, run_replay, run_watchlist_monitor
from futu_opend_execution.data.hshare_l2 import DEFAULT_HSHARE_L2_ROOT, HshareL2ReplayProvider
from futu_opend_execution.data.market import MarketEvent
from futu_opend_execution.data.opend_live import OpenDLiveProvider
from futu_opend_execution.execution.positions import OpenDPositionProvider
from futu_opend_execution.models import TradeMode
from futu_opend_execution.strategy_config import CostReducerRuntimeParams, config_to_jsonable
from futu_opend_execution.watchlist import WatchlistConfigError, load_watchlist_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenD Trading Agent: positions, L2 replay, paper ledger, monitor, and guarded auto-real.")
    sub = parser.add_subparsers(dest="command", required=True)

    positions = sub.add_parser("positions", help="Read HK positions from OpenD or print an offline empty snapshot")
    positions.add_argument("--real", action="store_true", help="Read real account positions; default uses simulated env")
    positions.add_argument("--offline", action="store_true", help="Do not connect OpenD; return an empty snapshot")

    replay = sub.add_parser("replay", help="Replay Hshare L2 parquet into strategy JSONL")
    _add_position_args(replay)
    replay.add_argument("--date", action="append", dest="dates", help="Trading date YYYY-MM-DD; can repeat")
    replay.add_argument("--data-root", default=str(DEFAULT_HSHARE_L2_ROOT))
    replay.add_argument("--interval-seconds", type=int, default=1)
    replay.add_argument("--limit-rows", type=int, default=None)
    replay.add_argument("--fixture", action="store_true", help="Use built-in synthetic L2 events for smoke tests")
    replay.add_argument("--log-path", default="logs/agent/replay.jsonl")

    paper = sub.add_parser("paper", help="Build paper ledger/report from replay JSONL")
    paper.add_argument("replay_log")
    paper.add_argument("--ledger-path", default="logs/agent/paper_ledger.jsonl")
    paper.add_argument("--report-path", default="reports/agent/paper_summary.json")

    monitor = sub.add_parser("monitor", help="Live OpenD dry-run monitor")
    monitor.add_argument("symbol", nargs="?", help="HK symbol, e.g. HK.00700; omit when --config is used")
    monitor.add_argument("--config", help="Watchlist JSON config")
    monitor.add_argument("--mode", default="live-dry-run", choices=["live-dry-run", "paper"])
    monitor.add_argument("--current-qty", type=int, default=None)
    monitor.add_argument("--cost-price", default=None)
    monitor.add_argument("--lot-size", type=int, default=None)
    monitor.add_argument("--max-sell-qty-per-order", type=int, default=None)
    monitor.add_argument("--max-rebuy-qty-per-order", type=int, default=None)
    monitor.add_argument("--iterations", type=int, default=1)
    monitor.add_argument("--interval-seconds", type=float, default=1.0)
    monitor.add_argument("--fake", action="store_true", help="Use a synthetic quote provider")
    monitor.add_argument("--log-path", default="logs/agent/monitor.jsonl")

    watchlist = sub.add_parser("watchlist", help="Validate and display watchlist JSON")
    watch_sub = watchlist.add_subparsers(dest="watchlist_command", required=True)
    validate = watch_sub.add_parser("validate")
    validate.add_argument("--config", required=True)
    show = watch_sub.add_parser("show")
    show.add_argument("--config", required=True)

    auto_real = sub.add_parser("auto-real", help="Validate auto-real gates; real execution remains disabled by default")
    auto_real.add_argument("--confirm-text", default="")
    auto_real.add_argument("--print-config", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "positions":
        return _cmd_positions(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "paper":
        print(json.dumps(run_paper(replay_log_path=args.replay_log, ledger_path=args.ledger_path, report_path=args.report_path), ensure_ascii=False))
        return 0
    if args.command == "monitor":
        return _cmd_monitor(args)
    if args.command == "watchlist":
        return _cmd_watchlist(args)
    if args.command == "auto-real":
        print(json.dumps({"auto_real_enabled": False, "reason": "LIVE_REAL_COST_REDUCER_AUTO is experimental and disabled by default"}, ensure_ascii=False))
        if args.print_config:
            print(json.dumps(config_to_jsonable(CostReducerRuntimeParams()), ensure_ascii=False))
        return 2
    raise AssertionError(args.command)


def _cmd_positions(args) -> int:
    if args.offline:
        print(json.dumps({"positions": [], "offline": True}, ensure_ascii=False))
        return 0
    with OpenDPositionProvider() as provider:
        positions = provider.list_positions(trade_mode=TradeMode.REAL if args.real else TradeMode.SIMULATED)
    print(json.dumps({"positions": [asdict(position) for position in positions]}, default=str, ensure_ascii=False))
    return 0


def _cmd_replay(args) -> int:
    config = _config_from_args(args)
    if args.fixture:
        provider = HshareL2ReplayProvider.from_events(_fixture_events(config.symbol), interval_seconds=args.interval_seconds)
    else:
        if not args.dates:
            raise SystemExit("replay requires --date unless --fixture is used")
        provider = HshareL2ReplayProvider(
            data_root=args.data_root,
            dates=args.dates,
            symbols=[config.symbol],
            interval_seconds=args.interval_seconds,
            limit_rows=args.limit_rows,
        )
    summary = run_replay(config=config, market_states=provider.iter_market_states(), log_path=args.log_path)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _cmd_monitor(args) -> int:
    if args.config:
        try:
            watchlist = load_watchlist_config(args.config)
        except WatchlistConfigError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        symbols = [item.symbol for item in watchlist.enabled_symbols]
        provider = _FakeLiveProvider(symbols) if args.fake else OpenDLiveProvider(symbols)
        try:
            events = run_watchlist_monitor(
                watchlist=watchlist,
                provider=provider,
                log_path=args.log_path,
                iterations=args.iterations,
                interval_seconds=args.interval_seconds,
                mode=args.mode,
            )
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        print(json.dumps({"events": events}, ensure_ascii=False))
        return 0

    config = _config_from_args(args)
    provider = _FakeLiveProvider(config.symbol) if args.fake else OpenDLiveProvider(config.symbol)
    try:
        events = run_monitor(
            config=config,
            provider=provider,
            log_path=args.log_path,
            iterations=args.iterations,
            interval_seconds=args.interval_seconds,
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    print(json.dumps({"events": events}, ensure_ascii=False))
    return 0


def _cmd_watchlist(args) -> int:
    try:
        config = load_watchlist_config(args.config)
    except WatchlistConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.watchlist_command == "validate":
        print(json.dumps({"ok": True, "symbols": [item.symbol for item in config.symbols]}, ensure_ascii=False))
        return 0
    if args.watchlist_command == "show":
        print(json.dumps(config.to_jsonable(), ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(args.watchlist_command)


def _add_position_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("symbol", help="HK symbol, e.g. HK.00700")
    parser.add_argument("--current-qty", type=int, required=True)
    parser.add_argument("--cost-price", required=True)
    parser.add_argument("--lot-size", type=int, required=True)
    parser.add_argument("--max-sell-qty-per-order", type=int, default=None)
    parser.add_argument("--max-rebuy-qty-per-order", type=int, default=None)


def _config_from_args(args) -> TradingAgentConfig:
    missing = [name for name in ("symbol", "current_qty", "cost_price", "lot_size") if getattr(args, name, None) in {None, ""}]
    if missing:
        raise SystemExit(f"missing required argument(s) without --config: {', '.join('--' + name.replace('_', '-') for name in missing)}")
    return TradingAgentConfig(
        symbol=args.symbol,
        current_qty=args.current_qty,
        cost_price=Decimal(str(args.cost_price)),
        lot_size=args.lot_size,
        max_sell_qty_per_order=args.max_sell_qty_per_order,
        max_rebuy_qty_per_order=args.max_rebuy_qty_per_order,
    )


def _fixture_events(symbol: str) -> list[MarketEvent]:
    start = datetime(2026, 5, 21, 9, 30)
    events: list[MarketEvent] = []
    for offset in range(20):
        ts = start + timedelta(seconds=offset)
        events.append(MarketEvent(symbol=symbol, timestamp=ts, event_type="trade", price=100, volume=1000))
        events.append(MarketEvent(symbol=symbol, timestamp=ts, event_type="book", bid_price="99.9", bid_size=1000, ask_price="100.0", ask_size=1000))
    events.extend(
        [
            MarketEvent(symbol=symbol, timestamp=start + timedelta(seconds=20), event_type="trade", price=103, volume=1000),
            MarketEvent(symbol=symbol, timestamp=start + timedelta(seconds=20), event_type="book", bid_price="102.9", bid_size=1, ask_price="103.0", ask_size=1000),
            MarketEvent(symbol=symbol, timestamp=start + timedelta(seconds=21), event_type="trade", price="102.5", volume=1000),
            MarketEvent(symbol=symbol, timestamp=start + timedelta(seconds=21), event_type="book", bid_price="102.4", bid_size=1, ask_price="102.5", ask_size=1000),
        ]
    )
    return events


class _FakeLiveProvider:
    def __init__(self, symbols: str | Sequence[str]) -> None:
        from futu_opend_execution.data.market import build_market_states

        if isinstance(symbols, str):
            symbols = [symbols]
        self._states = {
            symbol: build_market_states(_fixture_events(symbol), interval_seconds=1, source="fake_live")
            for symbol in symbols
        }
        self._index = {symbol: max(len(states) - 5, 0) for symbol, states in self._states.items()}

    def read_once(self, symbol: str | None = None):
        target = symbol or next(iter(self._states))
        states = self._states[target]
        index = min(self._index[target], len(states) - 1)
        self._index[target] += 1
        return states[index]


if __name__ == "__main__":
    raise SystemExit(main())
