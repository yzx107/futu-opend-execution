# OpenD Trading Agent

This repository is a Hong Kong Futu OpenD trading-agent skeleton. The agent does not pick stocks, recommend new positions, predict returns, or promise profit. Its first product goal is narrower: after the user has chosen held or watchlisted HK symbols, keep monitoring them, detect abnormal risk states, and dry-run / paper-test cost-reducer signals around existing inventory.

`BlackSwanSentinel` does not predict black swans. It detects abnormal market/data states, emits audit logs, pauses trading, alerts, requires manual review, and keeps a paper trail for later validation.

Default mode is always dry-run / paper-only. PR1 adds no real-order path.

## Safety Model

- OpenD must be reached through local loopback by default (`127.0.0.1`, `localhost`, or `::1`).
- Live market data uses the official Futu SDK `OpenQuoteContext`; strategies do not import the SDK.
- Cost reducer automation can only sell the trading bucket or rebuy previously sold trading inventory.
- Core inventory cannot be sold by the strategy.
- The strategy cannot open new positions and does not use market orders.
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

## Deferred to PR2

Real order submission, `place_order`, `modify_order`, account-specific approval flows, live-real execution, and any web/CLI control that could submit or cancel real orders are intentionally deferred to PR2.
