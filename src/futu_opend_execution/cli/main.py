"""CLI for the OpenD Trading Agent."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from futu_opend_execution.agent.approval import (
    approval_static_validation_errors,
    draft_approval_from_strategy_signal,
    load_approval_file,
)
from futu_opend_execution.agent.newly_listed import (
    build_newly_listed_universe,
    optimize_newly_listed,
    write_newly_listed_reports,
)
from futu_opend_execution.agent.optimizer import CostReducerGrid, optimize_cost_reducer, write_optimizer_reports
from futu_opend_execution.agent.real_execution import RealExecutionService
from futu_opend_execution.agent.risk import RealOrderGuard
from futu_opend_execution.agent.runtime import TradingAgentConfig, run_monitor, run_paper, run_replay, run_watchlist_monitor
from futu_opend_execution.contracts import ContractSpecError, load_contract_specs
from futu_opend_execution.data.hshare_l2 import DEFAULT_HSHARE_L2_ROOT, HshareL2ReplayProvider
from futu_opend_execution.data.hshare_top_of_book import HshareTopOfBookReplayProvider
from futu_opend_execution.data.market import MarketEvent
from futu_opend_execution.data.opend_live import OpenDLiveProvider
from futu_opend_execution.execution.positions import OpenDPositionProvider
from futu_opend_execution.ledger.futures import FuturesLedgerError, FuturesPaperLedger, summarize_futures_paper_ledger
from futu_opend_execution.models import BrokerOrderSnapshot, BrokerOrderStatus, TradeMode
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
    replay.add_argument(
        "--top-of-book-root",
        default=None,
        help="Use Hshare Lab v2 orderbook_replay__top_of_book_only output instead of raw candidate_cleaned.",
    )
    replay.add_argument("--interval-seconds", type=int, default=1)
    replay.add_argument("--limit-rows", type=int, default=None)
    replay.add_argument("--fixture", action="store_true", help="Use built-in synthetic L2 events for smoke tests")
    replay.add_argument("--log-path", default="logs/agent/replay.jsonl")

    optimize = sub.add_parser("optimize-cost-reducer", help="Grid-search dry-run cost reducer parameters")
    _add_position_args(optimize)
    optimize.add_argument("--date", action="append", dest="dates", help="Trading date YYYY-MM-DD; can repeat")
    optimize.add_argument("--data-root", default=str(DEFAULT_HSHARE_L2_ROOT))
    optimize.add_argument("--top-of-book-root", default=None)
    optimize.add_argument("--interval-seconds", type=int, default=1)
    optimize.add_argument("--limit-rows", type=int, default=None)
    optimize.add_argument("--fixture", action="store_true")
    optimize.add_argument("--top-n", type=int, default=20)
    optimize.add_argument("--report-json", default="reports/agent/optimizer_summary.json")
    optimize.add_argument("--report-md", default="reports/agent/optimizer_rank.md")
    optimize.add_argument("--overextension-grid", default=None)
    optimize.add_argument("--pullback-grid", default=None)
    optimize.add_argument("--rebuy-anchor-grid", default=None)
    optimize.add_argument("--safety-buffer-grid", default=None)
    optimize.add_argument("--max-sell-ratio-grid", default=None)
    optimize.add_argument("--max-round-trips-grid", default=None)

    universe = sub.add_parser("newly-listed-universe", help="Build a 2026 newly listed HK stock research universe")
    universe.add_argument("--listing-year", type=int, default=2026)
    universe.add_argument("--as-of", default=None)
    universe.add_argument("--instrument-profile", default="/Volumes/Data/港股Tick数据/reference/instrument_profile/latest/instrument_profile.parquet")
    universe.add_argument("--data-root", default=str(DEFAULT_HSHARE_L2_ROOT))
    universe.add_argument("--top-of-book-root", default=None)
    universe.add_argument("--date", action="append", dest="dates")
    universe.add_argument("--min-trade-dates", type=int, default=1)
    universe.add_argument("--max-symbols", type=int, default=None)
    universe.add_argument("--include-non-stock-candidates", action="store_true")
    universe.add_argument("--output-json", default="reports/agent/newly_listed_universe.json")
    universe.add_argument("--output-md", default="reports/agent/newly_listed_universe.md")

    optimize_new = sub.add_parser("optimize-newly-listed", help="Batch optimize cost reducer parameters for newly listed HK candidates")
    optimize_new.add_argument("--listing-year", type=int, default=2026)
    optimize_new.add_argument("--instrument-profile", default="/Volumes/Data/港股Tick数据/reference/instrument_profile/latest/instrument_profile.parquet")
    optimize_new.add_argument("--data-root", default=str(DEFAULT_HSHARE_L2_ROOT))
    optimize_new.add_argument("--top-of-book-root", default=None)
    optimize_new.add_argument("--date", action="append", dest="dates")
    optimize_new.add_argument("--min-trade-dates", type=int, default=1)
    optimize_new.add_argument("--max-symbols", type=int, default=20)
    optimize_new.add_argument("--max-dates-per-symbol", type=int, default=3)
    optimize_new.add_argument("--lot-size", type=int, default=1)
    optimize_new.add_argument("--current-qty", type=int, default=2)
    optimize_new.add_argument("--top-n", type=int, default=20)
    optimize_new.add_argument("--report-json", default="reports/agent/newly_listed_optimizer_summary.json")
    optimize_new.add_argument("--report-md", default="reports/agent/newly_listed_optimizer_rank.md")
    optimize_new.add_argument("--overextension-grid", default=None)
    optimize_new.add_argument("--pullback-grid", default=None)
    optimize_new.add_argument("--rebuy-anchor-grid", default=None)
    optimize_new.add_argument("--safety-buffer-grid", default=None)
    optimize_new.add_argument("--max-sell-ratio-grid", default=None)
    optimize_new.add_argument("--max-round-trips-grid", default=None)

    paper = sub.add_parser("paper", help="Build paper ledger/report from replay JSONL")
    paper.add_argument("replay_log")
    paper.add_argument("--ledger-path", default="logs/agent/paper_ledger.jsonl")
    paper.add_argument("--report-path", default="reports/agent/paper_summary.json")

    futures = sub.add_parser("futures", help="Futures contract specs and paper-ledger utilities")
    futures_sub = futures.add_subparsers(dest="futures_command", required=True)
    futures_contracts = futures_sub.add_parser("contracts", help="Validate and show futures contract specs")
    futures_contracts.add_argument("--config", default="configs/futures_contracts.example.json")
    futures_fill = futures_sub.add_parser("paper-fill", help="Append one futures paper fill")
    futures_fill.add_argument("symbol")
    futures_fill.add_argument("action", choices=["BUY_OPEN", "SELL_OPEN", "SELL_CLOSE", "BUY_CLOSE"])
    futures_fill.add_argument("--quantity", type=int, required=True)
    futures_fill.add_argument("--price", required=True)
    futures_fill.add_argument("--contracts-config", default="configs/futures_contracts.example.json")
    futures_fill.add_argument("--ledger-path", default="logs/agent/futures_paper_ledger.jsonl")
    futures_fill.add_argument("--event-id", default=None)
    futures_fill.add_argument("--timestamp", default=None)
    futures_fill.add_argument("--reason", default="")
    futures_summary = futures_sub.add_parser("paper-summary", help="Summarize a futures paper ledger")
    futures_summary.add_argument("--contracts-config", default="configs/futures_contracts.example.json")
    futures_summary.add_argument("--ledger-path", default="logs/agent/futures_paper_ledger.jsonl")
    futures_summary.add_argument("--mark", action="append", default=[], help="Mark price as SYMBOL=PRICE; can repeat")

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

    real = sub.add_parser("real", help="Manual approval-file real-order workflow")
    real_sub = real.add_subparsers(dest="real_command", required=True)
    draft_approval = real_sub.add_parser("draft-approval", help="Draft an approval file from a strategy_signal JSONL row")
    draft_approval.add_argument("--signal-log", required=True)
    draft_approval.add_argument("--output", required=True)
    draft_approval.add_argument("--signal-id", default=None)
    draft_approval.add_argument("--expires-minutes", type=int, default=5)
    draft_approval.add_argument("--max-spread-bps", default="20")

    validate_approval = real_sub.add_parser("validate-approval", help="Validate an approval file without connecting OpenD")
    validate_approval.add_argument("--approval-file", required=True)
    validate_approval.add_argument("--kill-switch-file", default="logs/agent/KILL_SWITCH")

    submit_approved = real_sub.add_parser("submit-approved", help="Submit an already approved limit order through all real-order gates")
    submit_approved.add_argument("--approval-file", required=True)
    submit_approved.add_argument("--confirm-text", required=True)
    submit_approved.add_argument("--audit-log", default="logs/agent/real_orders.jsonl")
    submit_approved.add_argument("--kill-switch-file", default="logs/agent/KILL_SWITCH")
    submit_approved.add_argument("--max-qty", type=int, required=True)
    submit_approved.add_argument("--max-notional", required=True)
    submit_approved.add_argument("--lot-size", type=int, default=None)
    submit_approved.add_argument("--timeout-seconds", type=float, default=1.0)
    submit_approved.add_argument("--poll-interval-seconds", type=float, default=0.2)
    submit_approved.add_argument("--fake-broker", action="store_true", help="Use a local fake broker for tests; never connects OpenD")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "positions":
        return _cmd_positions(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "optimize-cost-reducer":
        return _cmd_optimize_cost_reducer(args)
    if args.command == "newly-listed-universe":
        return _cmd_newly_listed_universe(args)
    if args.command == "optimize-newly-listed":
        return _cmd_optimize_newly_listed(args)
    if args.command == "paper":
        print(json.dumps(run_paper(replay_log_path=args.replay_log, ledger_path=args.ledger_path, report_path=args.report_path), ensure_ascii=False))
        return 0
    if args.command == "futures":
        return _cmd_futures(args)
    if args.command == "monitor":
        return _cmd_monitor(args)
    if args.command == "watchlist":
        return _cmd_watchlist(args)
    if args.command == "auto-real":
        print(json.dumps({"auto_real_enabled": False, "reason": "LIVE_REAL_COST_REDUCER_AUTO is experimental and disabled by default"}, ensure_ascii=False))
        if args.print_config:
            print(json.dumps(config_to_jsonable(CostReducerRuntimeParams()), ensure_ascii=False))
        return 2
    if args.command == "real":
        return _cmd_real(args)
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
    provider = _replay_provider_from_args(args, config)
    summary = run_replay(config=config, market_states=provider.iter_market_states(), log_path=args.log_path)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _cmd_optimize_cost_reducer(args) -> int:
    config = _config_from_args(args)
    provider = _replay_provider_from_args(args, config)
    default_grid = CostReducerGrid()
    grid = CostReducerGrid(
        overextension_vol_multiple=_decimal_tuple(args.overextension_grid, default_grid.overextension_vol_multiple),
        high_pullback_vol_multiple=_decimal_tuple(args.pullback_grid, default_grid.high_pullback_vol_multiple),
        rebuy_anchor_vol_band=_decimal_tuple(args.rebuy_anchor_grid, default_grid.rebuy_anchor_vol_band),
        safety_buffer_bps=_decimal_tuple(args.safety_buffer_grid, default_grid.safety_buffer_bps),
        max_sell_total_position_ratio=_decimal_tuple(args.max_sell_ratio_grid, default_grid.max_sell_total_position_ratio),
        max_round_trips=_int_tuple(args.max_round_trips_grid, default_grid.max_round_trips),
    )
    summary = optimize_cost_reducer(
        config=config,
        market_states=provider.iter_market_states(),
        grid=grid,
        top_n=args.top_n,
    )
    write_optimizer_reports(summary, json_path=args.report_json, markdown_path=args.report_md)
    print(json.dumps({key: summary[key] for key in ("event", "symbol", "market_state_count", "grid_size", "top_n")}, ensure_ascii=False))
    return 0


def _cmd_newly_listed_universe(args) -> int:
    as_of = datetime.fromisoformat(args.as_of).date() if args.as_of else None
    summary = build_newly_listed_universe(
        instrument_profile_path=args.instrument_profile,
        data_root=args.data_root,
        top_of_book_root=args.top_of_book_root,
        listing_year=args.listing_year,
        as_of=as_of,
        dates=args.dates,
        min_trade_dates=args.min_trade_dates,
        stock_research_candidate_only=not args.include_non_stock_candidates,
        max_symbols=args.max_symbols,
    )
    write_newly_listed_reports(summary, json_path=args.output_json, markdown_path=args.output_md)
    print(json.dumps({key: summary[key] for key in ("event", "listing_year", "candidate_count")}, ensure_ascii=False))
    return 0


def _cmd_optimize_newly_listed(args) -> int:
    default_grid = CostReducerGrid()
    grid = CostReducerGrid(
        overextension_vol_multiple=_decimal_tuple(args.overextension_grid, default_grid.overextension_vol_multiple),
        high_pullback_vol_multiple=_decimal_tuple(args.pullback_grid, default_grid.high_pullback_vol_multiple),
        rebuy_anchor_vol_band=_decimal_tuple(args.rebuy_anchor_grid, default_grid.rebuy_anchor_vol_band),
        safety_buffer_bps=_decimal_tuple(args.safety_buffer_grid, default_grid.safety_buffer_bps),
        max_sell_total_position_ratio=_decimal_tuple(args.max_sell_ratio_grid, default_grid.max_sell_total_position_ratio),
        max_round_trips=_int_tuple(args.max_round_trips_grid, default_grid.max_round_trips),
    )
    summary = optimize_newly_listed(
        instrument_profile_path=args.instrument_profile,
        data_root=args.data_root,
        top_of_book_root=args.top_of_book_root,
        listing_year=args.listing_year,
        dates=args.dates,
        min_trade_dates=args.min_trade_dates,
        max_symbols=args.max_symbols,
        max_dates_per_symbol=args.max_dates_per_symbol,
        lot_size=args.lot_size,
        current_qty=args.current_qty,
        grid=grid,
        top_n=args.top_n,
    )
    write_newly_listed_reports(summary, json_path=args.report_json, markdown_path=args.report_md)
    print(json.dumps({key: summary[key] for key in ("event", "listing_year", "evaluated_case_count", "result_row_count", "failure_count")}, ensure_ascii=False))
    return 0


def _cmd_futures(args) -> int:
    try:
        contracts = load_contract_specs(args.config if args.futures_command == "contracts" else args.contracts_config)
        if args.futures_command == "contracts":
            payload = {
                "event": "futures_contracts",
                "contract_count": len(contracts),
                "contracts": [spec.to_jsonable() for spec in contracts.values()],
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        if args.futures_command == "paper-fill":
            ledger = FuturesPaperLedger(args.ledger_path, contracts=contracts)
            row = ledger.record_fill(
                symbol=args.symbol,
                action=args.action,
                quantity=args.quantity,
                price=args.price,
                timestamp=args.timestamp,
                event_id=args.event_id,
                reason=args.reason,
            )
            print(json.dumps({"ok": True, "row": row}, ensure_ascii=False))
            return 0
        if args.futures_command == "paper-summary":
            summary = summarize_futures_paper_ledger(args.ledger_path, contracts=contracts, mark_prices=_parse_marks(args.mark))
            print(json.dumps(summary, ensure_ascii=False))
            return 0
    except (ContractSpecError, FuturesLedgerError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    raise AssertionError(args.futures_command)


def _replay_provider_from_args(args, config: TradingAgentConfig):
    if args.fixture:
        return HshareL2ReplayProvider.from_events(_fixture_events(config.symbol), interval_seconds=args.interval_seconds)
    if not args.dates:
        raise SystemExit(f"{args.command} requires --date unless --fixture is used")
    if args.top_of_book_root:
        return HshareTopOfBookReplayProvider(
            data_root=args.top_of_book_root,
            dates=args.dates,
            symbols=[config.symbol],
            interval_seconds=args.interval_seconds,
            limit_rows=args.limit_rows,
        )
    return HshareL2ReplayProvider(
        data_root=args.data_root,
        dates=args.dates,
        symbols=[config.symbol],
        interval_seconds=args.interval_seconds,
        limit_rows=args.limit_rows,
    )


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


def _cmd_real(args) -> int:
    if args.real_command == "draft-approval":
        return _cmd_real_draft_approval(args)

    if args.real_command == "validate-approval":
        approval = load_approval_file(args.approval_file)
        errors = approval_static_validation_errors(approval)
        print(json.dumps({"ok": not errors, "layer": "static", "approval_id": approval.approval_id, "errors": errors}, ensure_ascii=False))
        return 0 if not errors else 2

    if args.real_command == "submit-approved":
        approval = load_approval_file(args.approval_file)
        guard = RealOrderGuard(
            allow_real_trade=True,
            kill_switch_file=Path(args.kill_switch_file),
            max_qty=args.max_qty,
            max_notional=Decimal(str(args.max_notional)),
            lot_size=args.lot_size or approval.lot_size,
        )
        broker = _CliFakeBroker() if args.fake_broker else None
        service = RealExecutionService(
            broker=broker,
            guard=guard,
            audit_log_path=args.audit_log,
            poll_interval_seconds=args.poll_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        try:
            summary = service.submit_approval(approval, confirm_text=args.confirm_text)
        finally:
            service.close()
        print(json.dumps(summary.to_jsonable(), ensure_ascii=False))
        return 0 if summary.ok else 2

    raise AssertionError(args.real_command)


def _cmd_real_draft_approval(args) -> int:
    rows = _read_jsonl_rows(args.signal_log)
    signal = _select_strategy_signal(rows, signal_id=args.signal_id)
    if signal is None:
        print(json.dumps({"ok": False, "error": "strategy_signal not found"}, ensure_ascii=False))
        return 2
    try:
        draft = draft_approval_from_strategy_signal(
            signal,
            expires_minutes=args.expires_minutes,
            max_spread_bps=args.max_spread_bps,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "approval_id": draft["approval_id"], "output": str(output)}, ensure_ascii=False))
    return 0


def _read_jsonl_rows(path: str | Path) -> list[dict[str, object]]:
    target = Path(path)
    rows: list[dict[str, object]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _select_strategy_signal(
    rows: list[dict[str, object]],
    *,
    signal_id: str | None,
) -> dict[str, object] | None:
    candidates = [row for row in rows if row.get("event") == "strategy_signal"]
    if signal_id:
        for row in reversed(candidates):
            if row.get("signal_id") == signal_id or row.get("client_intent_id") == signal_id:
                return row
        return None
    return candidates[-1] if candidates else None


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


def _decimal_tuple(value: str | None, default: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    if not value:
        return default
    return tuple(Decimal(item.strip()) for item in value.split(",") if item.strip())


def _int_tuple(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    if not value:
        return default
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def _parse_marks(values: Sequence[str]) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for value in values:
        if "=" not in value:
            raise FuturesLedgerError("--mark must use SYMBOL=PRICE")
        symbol, price = value.split("=", 1)
        marks[symbol.strip().upper()] = Decimal(price.strip())
    return marks


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


class _CliFakeBroker:
    supports_native_ioc = False

    def __init__(self) -> None:
        self._latest: BrokerOrderSnapshot | None = None

    def place_limit_buy(self, **kwargs) -> BrokerOrderSnapshot:
        return self._place(kwargs)

    def place_limit_sell(self, **kwargs) -> BrokerOrderSnapshot:
        return self._place(kwargs)

    def get_order(self, *, order_id: str, symbol: str, trade_mode: TradeMode) -> BrokerOrderSnapshot:
        del order_id, symbol, trade_mode
        if self._latest is None:
            raise RuntimeError("fake broker order missing")
        return self._latest

    def cancel_order(self, *, order_id: str, symbol: str, trade_mode: TradeMode) -> None:
        del order_id, symbol, trade_mode

    def close(self) -> None:
        return None

    def _place(self, kwargs) -> BrokerOrderSnapshot:
        del kwargs["time_in_force"], kwargs["trade_mode"], kwargs["remark"]
        self._latest = BrokerOrderSnapshot(
            order_id="fake-order-1",
            symbol=kwargs["symbol"],
            status=BrokerOrderStatus.FILLED_ALL,
            quantity=kwargs["quantity"],
            price=kwargs["limit_price"],
            dealt_quantity=kwargs["quantity"],
            dealt_avg_price=kwargs["limit_price"],
            updated_time=datetime.now().isoformat(),
        )
        return self._latest


if __name__ == "__main__":
    raise SystemExit(main())
