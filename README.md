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

Submit an already approved file through all real-order gates:

```bash
FUTU_ALLOW_REAL_TRADE=1 \
PYTHONPATH=src python -m futu_opend_execution.cli.main real submit-approved \
  --approval-file approvals/example_approval.json \
  --confirm-text 确认实盘 \
  --audit-log logs/agent/real_orders.jsonl
```

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

## Development

```bash
python -m pytest -q
bash scripts/agent_smoke.sh
```

If your shell has no `python` binary, use `.venv/bin/python` or `python3`.

## Real-Money Caveats

Before any real-money use, manually verify the local OpenD version, `FUTU_ACC_ID`, unlock behavior, order status values, partial-fill timing, and cancel behavior in the target account. `futu-api` remains optional for tests; unit tests use fake brokers and must not connect to real OpenD.
