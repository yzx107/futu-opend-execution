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
  "${ROOT}/reports/agent/smoke_paper_summary.json"

"${PY}" -m futu_opend_execution.cli.main positions --offline
"${PY}" -m futu_opend_execution.cli.main replay HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --fixture \
  --log-path "${ROOT}/logs/agent/smoke_replay.jsonl"
"${PY}" -m futu_opend_execution.cli.main paper "${ROOT}/logs/agent/smoke_replay.jsonl" \
  --ledger-path "${ROOT}/logs/agent/smoke_paper_ledger.jsonl" \
  --report-path "${ROOT}/reports/agent/smoke_paper_summary.json"
"${PY}" -m futu_opend_execution.cli.main monitor HK.00700 \
  --current-qty 200 \
  --cost-price 100 \
  --lot-size 100 \
  --fake \
  --iterations 1 \
  --log-path "${ROOT}/logs/agent/smoke_monitor.jsonl"
