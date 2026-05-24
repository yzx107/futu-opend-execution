#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

export PYTHONPATH="${ROOT}/src"
mkdir -p "${ROOT}/logs/agent" "${ROOT}/reports/agent"
rm -f \
  "${ROOT}/logs/agent/smoke_replay.jsonl" \
  "${ROOT}/logs/agent/smoke_paper_ledger.jsonl" \
  "${ROOT}/logs/agent/smoke_monitor.jsonl" \
  "${ROOT}/logs/agent/smoke_futures_replay.jsonl" \
  "${ROOT}/logs/agent/smoke_futures_ledger.jsonl" \
  "${ROOT}/reports/agent/smoke_paper_summary.json" \
  "${ROOT}/reports/agent/smoke_optimizer_summary.json" \
  "${ROOT}/reports/agent/smoke_optimizer_rank.md"

"${PY}" -m futu_opend_execution.cli.main positions --offline
"${PY}" -m futu_opend_execution.cli.main watchlist validate \
  --config "${ROOT}/configs/watchlist.example.json"
"${PY}" -m futu_opend_execution.cli.main watchlist show \
  --config "${ROOT}/configs/watchlist.example.json" >/dev/null
"${PY}" -m futu_opend_execution.cli.main replay HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --fixture \
  --log-path "${ROOT}/logs/agent/smoke_replay.jsonl"
"${PY}" -m futu_opend_execution.cli.main paper "${ROOT}/logs/agent/smoke_replay.jsonl" \
  --ledger-path "${ROOT}/logs/agent/smoke_paper_ledger.jsonl" \
  --report-path "${ROOT}/reports/agent/smoke_paper_summary.json"
"${PY}" -m futu_opend_execution.cli.main optimize-cost-reducer HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --fixture \
  --overextension-grid 1.5,2.0 \
  --pullback-grid 0.3 \
  --rebuy-anchor-grid 1.0 \
  --safety-buffer-grid 20 \
  --max-sell-ratio-grid 0.5 \
  --report-json "${ROOT}/reports/agent/smoke_optimizer_summary.json" \
  --report-md "${ROOT}/reports/agent/smoke_optimizer_rank.md"
"${PY}" -m futu_opend_execution.cli.main futures replay HK.HSI2606 \
  --fixture \
  --contracts-config "${ROOT}/configs/futures_contracts.example.json" \
  --log-path "${ROOT}/logs/agent/smoke_futures_replay.jsonl" \
  --ledger-path "${ROOT}/logs/agent/smoke_futures_ledger.jsonl"
"${PY}" -m futu_opend_execution.cli.main monitor HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --fake \
  --iterations 1 \
  --log-path "${ROOT}/logs/agent/smoke_monitor.jsonl"
