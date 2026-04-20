# futu-opend-execution

Hong Kong Futu/OpenD execution layer focused on simulated execution first.

## Why this repo exists

This repository is intended to isolate Futu/OpenD order execution concerns from broader research code.

The initial goal is to build a small, testable execution layer that:
- talks to a local Futu OpenD gateway
- starts with simulated trading only
- keeps real-trade enablement behind an explicit hard gate
- is suitable for later extension to IPO first-day and grey-market workflows

## Scope

Current scope:
- Python package scaffold
- environment-based connection settings
- CI to ensure the package installs cleanly
- documentation for safe setup and future architecture

Planned scope:
- OpenD connection bootstrap
- account and trading-context adapters
- execution request / response models
- simulated order placement and status polling
- order validation and risk guardrails
- dry-run and paper-trading workflows

Out of scope for the early phase:
- unattended real-money trading by default
- strategy logic
- portfolio optimization
- signal generation
- credential storage in git

## Safety model

This project is intentionally simulation-first.

Environment switch:
- `FUTU_ALLOW_REAL_TRADE=0` means real trading must remain disabled
- any real-trade support should require explicit opt-in, separate checks, and additional safeguards

Design principles:
- fail closed
- validate inputs before sending orders
- log every execution intent and broker response
- separate strategy from execution
- keep credentials and environment-specific configuration out of version control

## Repository layout

```text
.
├── .env.example
├── .github/
│   └── workflows/
│       └── ci.yml
├── LICENSE
├── README.md
├── pyproject.toml
└── src/
    └── futu_opend_execution/
        └── __init__.py
```

Expected future expansion:

```text
src/futu_opend_execution/
├── client.py          # OpenD connectivity
├── config.py          # env parsing and runtime settings
├── models.py          # execution request / response models
├── risk.py            # validation and guardrails
├── execution/
│   ├── simulator.py
│   └── broker.py
└── services/
    └── orders.py
```

## Quick start

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
```

Default variables:

```env
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
FUTU_ALLOW_REAL_TRADE=0
```

### 3. Run local checks

```bash
python -m pip install --upgrade pip
pip install -e .
python -c "import futu_opend_execution; print('ok')"
```

## Development notes

- Requires Python 3.11+
- Assumes a local Futu OpenD instance is available when broker integration begins
- For now, the package is only a scaffold and intentionally contains no live execution logic

## Roadmap

### Phase 1: scaffold
- [x] create package skeleton
- [x] publish repository
- [x] add CI and basic docs

### Phase 2: simulated execution MVP
- [ ] config loader
- [ ] connection health check
- [ ] execution request model
- [ ] simulated order adapter
- [ ] structured logging

### Phase 3: broker integration
- [ ] OpenD trading context wrapper
- [ ] order placement abstraction
- [ ] order status reconciliation
- [ ] retry / timeout policy
- [ ] explicit real-trade guardrails

## License

MIT
