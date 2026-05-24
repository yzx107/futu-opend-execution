# OpenD Trading Agent

This repository is a Hong Kong Futu OpenD trading-agent skeleton. The agent does not pick stocks, recommend new positions, predict returns, or promise profit. Its first product goal is narrower: after the user has chosen held or watchlisted HK symbols, keep monitoring them, detect abnormal risk states, and dry-run / paper-test cost-reducer signals around existing inventory.

`BlackSwanSentinel` does not predict black swans. It detects abnormal market/data states, emits audit logs, pauses trading, alerts, requires manual review, and keeps a paper trail for later validation.

Default mode is always dry-run / paper-only. PR2 adds a guarded manual real-order path, but it is fail-closed and requires an approval file, confirmation phrase, kill-switch absence, loopback OpenD, and `FUTU_ALLOW_REAL_TRADE=1`.

## Safety Model

- OpenD must be reached through local loopback by default (`127.0.0.1`, `localhost`, or `::1`).
- Live market data uses the official Futu SDK `OpenQuoteContext`; strategies do not import the SDK.
- Cost reducer automation can only sell the trading bucket or rebuy previously sold trading inventory.
- Core inventory cannot be sold by the strategy.
- The strategy cannot open new positions and does not use market orders.
- Real orders are approval-file only, limit-order only, and manual only.
- `LIVE_REAL_COST_REDUCER_AUTO` is not enabled.
- `DRY_RUN_SIGNAL` is paper-only. `RISK_BLOCKED` is not consumed by the paper ledger.
- Kill switch creation remains available in the web console; clearing it requires `CLEAR_KILL_SWITCH` or a configured token.
- Paper results are simulation records and do not imply real fillability, slippage, liquidity, or profitability.

## Watchlist

Example config:

```bash
configs/watchlist.example.json
```

Validate and display normalized JSON:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main watchlist validate \
  --config configs/watchlist.example.json

PYTHONPATH=src python -m futu_opend_execution.cli.main watchlist show \
  --config configs/watchlist.example.json
```

The config fails fast on malformed HK symbols, non-positive or non-lot-aligned quantities, core/trading bucket mismatch, invalid sell ratio, negative risk thresholds, and missing required fields.

## Replay, Paper, Monitor

Replay Hshare L2 trades/orders into `MarketState` and strategy signals:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main replay HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --date 2026-05-21 \
  --data-root /Volumes/Data/港股Tick数据/candidate_cleaned \
  --log-path logs/agent/replay_00700.jsonl
```

Build an append-only paper ledger/report from replay output:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main paper logs/agent/replay_00700.jsonl \
  --ledger-path logs/agent/paper_ledger_00700.jsonl \
  --report-path reports/agent/paper_summary_00700.json
```

Grid-search a small cost-reducer parameter set:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main optimize-cost-reducer HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --date 2026-05-21 \
  --data-root /Volumes/Data/港股Tick数据/candidate_cleaned \
  --overextension-grid 1.5,2.0,2.5 \
  --pullback-grid 0.3,0.5,0.8 \
  --safety-buffer-grid 20,30 \
  --report-json reports/agent/optimizer_summary_00700.json \
  --report-md reports/agent/optimizer_rank_00700.md
```

The optimizer uses the same fail-closed cost reducer gates as replay. If top-of-book quality or depth is insufficient, the result will show blocked rows instead of inventing executable signals.

Read optimizer rankings defensively: rows with `open_quantity > 0` are unfinished sell legs, not completed high-sell/low-rebuy round trips. Prefer candidates with positive `realized_net_pnl`, completed `round_trips_completed`, low `open_quantity`, and low `quality_block_count`.

Build the 2026 newly listed HK research universe from the Hshare-native universe handoff and top-of-book coverage:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main newly-listed-universe \
  --listing-year 2026 \
  --universe-path /Volumes/Data/港股Tick数据/reference/newly_listed_hk/year=2026/newly_listed_hk_2026.parquet \
  --top-of-book-root /Volumes/Data/港股Tick数据/caveat/orderbook_replay__top_of_book_with_size_caveat \
  --output-json reports/agent/newly_listed_universe_2026_handoff.json \
  --output-md reports/agent/newly_listed_universe_2026_handoff.md
```

Run a bounded newly listed cost-reducer optimization:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main optimize-newly-listed \
  --listing-year 2026 \
  --universe-path /Volumes/Data/港股Tick数据/reference/newly_listed_hk/year=2026/newly_listed_hk_2026.parquet \
  --top-of-book-root /Volumes/Data/港股Tick数据/caveat/orderbook_replay__top_of_book_with_size_caveat \
  --date 2026-05-22 \
  --max-symbols 3 \
  --max-dates-per-symbol 1 \
  --overextension-grid 1.5,2.0 \
  --pullback-grid 0.3 \
  --rebuy-anchor-grid 1.0 \
  --safety-buffer-grid 20 \
  --max-sell-ratio-grid 0.5 \
  --report-json reports/agent/newly_listed_optimizer_smoke.json \
  --report-md reports/agent/newly_listed_optimizer_smoke.md
```

The newly listed optimizer is research/paper only. It reports `net_pnl_after_cost`, `cost_basis_reduction`, completed round trips, open-quantity penalty, and quality/risk block counts. With the Hshare-native universe, only `universe_status=included` rows are admitted. With the size-caveat top-of-book handoff, only `StrategyHandoffEligibleFlag=true` rows passing all replay-quality gates are executable. If Hshare rows do not provide strategy-grade bid/ask depth, the strategy stays blocked and the ranking is not a tradable parameter recommendation.

Run live dry-run monitor from watchlist:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main monitor \
  --config configs/watchlist.example.json \
  --mode live-dry-run \
  --log-path logs/agent/monitor.jsonl
```

Run deterministic fake monitor:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main monitor HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --fake \
  --iterations 5 \
  --log-path logs/agent/monitor_fake.jsonl
```

JSONL logs include `market_state`, `risk_event`, `strategy_signal`, `paper_trade`, `replay_summary`, and `paper_summary` rows. Sensitive trading passwords, tokens, account payloads, and raw account data must not be logged.

## Futures Foundation

Index-futures support is research/paper-only at this stage. The repo now has a separate contract spec and futures paper ledger so futures accounting does not leak into the stock cost-reducer inventory model.

Validate example futures contract specs:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main futures contracts \
  --config configs/futures_contracts.example.json
```

Probe OpenD futures contract metadata in read-only mode:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main futures opend-info HK.HSI2606 \
  --check-trade-context \
  --margin-rate 0.08 \
  --commission-per-contract 12
```

This calls OpenD quote `get_future_info` and can optionally open then close `OpenFutureTradeContext` without unlocking trade or submitting orders. The generated `contract_specs` are a local starting point; verify exchange multiplier, tick size, margin, fee, expiry, session, and rollover before any real use.

If OpenD is not listening on local loopback, the command fails fast before the Futu SDK enters its reconnect loop.

Append paper fills and summarize PnL / margin:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main futures paper-fill HK.HSI2606 BUY_OPEN \
  --quantity 1 \
  --price 20000 \
  --event-id demo-open \
  --contracts-config configs/futures_contracts.example.json \
  --ledger-path logs/agent/futures_paper_ledger.jsonl

PYTHONPATH=src python -m futu_opend_execution.cli.main futures paper-fill HK.HSI2606 SELL_CLOSE \
  --quantity 1 \
  --price 20010 \
  --event-id demo-close \
  --contracts-config configs/futures_contracts.example.json \
  --ledger-path logs/agent/futures_paper_ledger.jsonl

PYTHONPATH=src python -m futu_opend_execution.cli.main futures paper-summary \
  --contracts-config configs/futures_contracts.example.json \
  --ledger-path logs/agent/futures_paper_ledger.jsonl \
  --mark HK.HSI2606=20010
```

Run the first futures replay/paper strategy harness:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main futures replay HK.HSI2606 \
  --fixture \
  --contracts-config configs/futures_contracts.example.json \
  --log-path logs/agent/futures_replay.jsonl \
  --ledger-path logs/agent/futures_replay_ledger.jsonl
```

For CSV handoffs, provide rows with at least `timestamp`, `price`, `volume`, and preferably `bid_price`, `bid_size`, `ask_price`, `ask_size`:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main futures replay HK.HSI2606 \
  --csv data/futures_hsi2606_sample.csv \
  --contracts-config configs/futures_contracts.example.json \
  --max-contracts 1 \
  --max-daily-loss 1000 \
  --max-margin-used 100000
```

The first futures strategy is intentionally simple: VWAP-deviation mean reversion with `BUY_OPEN`, `SELL_OPEN`, `SELL_CLOSE`, `BUY_CLOSE`, and `WAIT`. It blocks stale/missing book data, wide spread, max contracts, daily loss, and max margin. It is a harness for replay/paper validation, not a live trading recommendation.

The futures foundation validates tick alignment, multiplier, min order size, commission, FIFO close PnL, open margin, and mark-to-market. It does not place futures orders, model exchange-specific tick ladders, handle rollover automatically, or validate live OpenD futures order behavior.

## Manual Real-Order Approval

The real path is stacked on PR1's dry-run / risk / signal semantics. PR2 consumes low-coupling approval snapshots rather than reimplementing PR1's `WatchlistConfig`, `MarketState`, `RiskEvent`, or `BlackSwanSentinel`.

The approval schema example is intentionally unsafe to execute: it is expired and `approved=false`.

```bash
approvals/example_approval.json
```

Validate an approval file without connecting to OpenD:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main real validate-approval \
  --approval-file approvals/example_approval.json
```

Draft an approval file from the latest executable `strategy_signal` JSONL row:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main real draft-approval \
  --signal-log logs/agent/monitor.jsonl \
  --output approvals/draft_00700.json
```

`draft-approval` refuses `source_signal_status=RISK_BLOCKED` and `source_signal_status=NOT_EXECUTABLE`. Drafts are always `approved=false` and have an empty confirmation phrase until an operator reviews and edits them.

Submit an already approved file through all real-order gates:

```bash
FUTU_ALLOW_REAL_TRADE=1 \
PYTHONPATH=src python -m futu_opend_execution.cli.main real submit-approved \
  --approval-file approvals/example_approval.json \
  --confirm-text 确认实盘 \
  --max-qty 100 \
  --max-notional 30000 \
  --audit-log logs/agent/real_orders.jsonl
```

`validate-approval` runs static schema/snapshot/source-signal checks only. `submit-approved` adds operator approval, expiration, confirmation phrase, kill-switch, explicit `--max-qty` / `--max-notional`, and `RealOrderGuard` checks immediately before broker submission.

`submit-approved` does not accept direct `symbol` / `quantity` / `price` inputs. The service builds a `RealOrderIntent` from the approval file, runs `RealOrderGuard` immediately before the broker call, submits only limit orders, polls order status, cancels on timeout, and reconciles confirmed partial fills. Cancelled unfilled orders do not update inventory. Fills observed after cancel produce `reconciliation_warning`.

## Web Console

Start the local console:

```bash
PYTHONPATH=src python -m futu_opend_execution.web_app \
  --host 127.0.0.1 \
  --port 8765 \
  --log-path logs/agent/monitor.jsonl \
  --watchlist-config configs/watchlist.example.json \
  --paper-report reports/agent/paper_summary_00700.json
```

The console shows service status, mode, watchlist, latest market state, latest strategy signal, latest risk event, kill switch state, and paper summary when available. Non-loopback hosts are rejected unless `--allow-non-loopback-dev` is explicitly set.

The console has no one-click real-order button.

## Hshare L2 Replay

`HshareL2ReplayProvider` reads:

```text
/Volumes/Data/港股Tick数据/candidate_cleaned/trades/date=YYYY-MM-DD/*.parquet
/Volumes/Data/港股Tick数据/candidate_cleaned/orders/date=YYYY-MM-DD/*.parquet
```

Trades map to `MarketEvent`. Orders are used only when side-like fields are explicit; if side is unavailable, replay continues with trade-derived VWAP/volatility and marks book-derived fields as limited/unavailable rather than fabricating spread or imbalance. See `docs/hshare_l2_replay.md` for the probe harness before using reconstructed order book fields.

When Hshare Lab v2 has materialized the bounded top-of-book handoff, replay can consume it explicitly:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main replay HK.01609 \
  --current-qty 30 \
  --cost-price 190 \
  --lot-size 15 \
  --date 2026-05-22 \
  --top-of-book-root /Volumes/Data/港股Tick数据/caveat/orderbook_replay__top_of_book_with_size_caveat \
  --log-path logs/agent/replay_01609_top_of_book.jsonl
```

Only rows passing the Hshare v2 quality gate are admitted as replay best bid/ask. Crossed, residue, excluded, same-millisecond-risk, invalid, or `StrategyHandoffEligibleFlag=false` rows are marked `book_quality=BLOCKED`. The size-caveat release includes bounded best-price size, not executable queue priority or full-depth fill realism, so cost-reducer execution remains fail-closed when quality is ambiguous.

## Development

```bash
python -m pytest -q
bash scripts/agent_smoke.sh
```

If your shell has no `python` binary, use `.venv/bin/python` or `python3`.

## Real-Money Caveats

Before any real-money use, manually verify the local OpenD version, `FUTU_ACC_ID`, unlock behavior, order status values, partial-fill timing, and cancel behavior in the target account. `futu-api` remains optional for tests; unit tests use fake brokers and must not connect to real OpenD.
