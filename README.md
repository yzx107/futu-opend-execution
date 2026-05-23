# OpenD Trading Agent

This repository is now a generic Hong Kong OpenD trading-agent skeleton. It keeps the package name `futu_opend_execution`, while the former IPO/open-buy prototype is no longer part of the main product surface.

The first version has one goal: **optimize cost around an existing HK position**. It does not predict new positions, does not promise profit, and defaults to dry-run.

## Safety Model

- Default mode is dry-run.
- Real trading requires `FUTU_ALLOW_REAL_TRADE=1`, local OpenD loopback, a confirmation phrase, kill switch absent, explicit max quantity/notional, and valid inventory.
- Cost reducer automation can only sell the trading bucket or rebuy previously sold trading inventory.
- Core inventory cannot be sold by the strategy.
- Market orders are not used.
- `LIVE_REAL_COST_REDUCER_AUTO` exists in the mode enum but remains disabled by default.

## Commands

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main positions --offline

PYTHONPATH=src python -m futu_opend_execution.cli.main replay HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --date 2026-05-21 \
  --data-root /Volumes/Data/港股Tick数据/candidate_cleaned \
  --log-path logs/agent/replay_00700.jsonl

PYTHONPATH=src python -m futu_opend_execution.cli.main paper logs/agent/replay_00700.jsonl \
  --ledger-path logs/agent/paper_ledger_00700.jsonl \
  --report-path reports/agent/paper_summary_00700.json

PYTHONPATH=src python -m futu_opend_execution.cli.main monitor HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --iterations 1 \
  --log-path logs/agent/monitor_00700.jsonl

PYTHONPATH=src python -m futu_opend_execution.web_app --port 8765
```

Smoke harness:

```bash
bash scripts/agent_smoke.sh
```

## Structure

```text
src/futu_opend_execution/
  agent/        runtime loops and real-order risk guard
  cli/          positions / replay / paper / monitor / auto-real commands
  data/         MarketEvent, MarketState, Hshare L2 replay, OpenD live provider
  execution/    Futu broker, positions, order-intent models
  ledger/       paper ledger and summaries
  strategies/   cost reducer strategy interface
  web/          simple local status console
```

## Hshare L2 Replay

`HshareL2ReplayProvider` reads:

```text
/Volumes/Data/港股Tick数据/candidate_cleaned/trades/date=YYYY-MM-DD/*.parquet
/Volumes/Data/港股Tick数据/candidate_cleaned/orders/date=YYYY-MM-DD/*.parquet
```

It maps rows into `MarketEvent`, then builds 1s/3s/5s `MarketState` windows for strategy and paper-ledger validation.

## Development

```bash
python -m unittest discover -s tests
python -m pytest -q
bash scripts/agent_smoke.sh
```

If your shell has no `python` binary, use `.venv/bin/python` or `python3`.
