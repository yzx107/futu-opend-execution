# Hshare L2 Replay

The first replay adapter reads Hshare Lab v2 `candidate_cleaned` parquet:

```text
/Volumes/Data/港股Tick数据/candidate_cleaned/trades/date=YYYY-MM-DD/*.parquet
/Volumes/Data/港股Tick数据/candidate_cleaned/orders/date=YYYY-MM-DD/*.parquet
```

Expected useful columns:

- `SendTime`
- `Price`
- `Volume`
- `Dir` or side-like fields when available
- `source_file`, used to derive the HK stock code

Example:

```bash
PYTHONPATH=src python -m futu_opend_execution.cli.main replay HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --date 2026-05-21 \
  --interval-seconds 1 \
  --log-path logs/agent/replay_00700.jsonl
```

If the order parquet does not expose bid/ask side, replay still produces trade-derived VWAP/volatility states, but spread and imbalance will be limited. That is a data limitation, not a trading signal.

## Order Book Semantics Probe

Hshare 2026 `orders` rows carry vendor-defined lifecycle fields such as `OrderType`, `Ext`, `Level`, `OrderId`, and `VolumePre`. Before using those rows as strategy-grade bid/ask depth, run the probe harness:

```bash
PYTHONPATH=src python scripts/probe_hshare_orderbook.py HK.01879 \
  --date 2026-05-22 \
  --data-root /Volumes/Data/港股Tick数据/candidate_cleaned
```

The probe tests both likely `Ext` side bits, rebuilds active order lifecycle, and scores candidates with crossed-book rate, `BidOrderID` / `AskOrderID` side matches, `VolumePre` checks, and level consistency. Reports are written to `reports/agent/`.

Treat the result as evidence only. Wire reconstructed depth into replay only after the probe shows acceptable linkage and book sanity for the symbols/dates you trade.
