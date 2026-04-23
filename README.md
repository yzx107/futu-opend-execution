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
- grey-market buy-at-open planning logic
- visible-order-book simulation for buy orders
- broker submission workflow with cancel-on-timeout execution
- CI to ensure the package installs and tests cleanly
- documentation for safe setup and future architecture

Planned scope:
- OpenD connection bootstrap
- account and trading-context adapters
- broker-backed order placement and status polling
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
├── setup.py
├── tests/
│   ├── test_greymarket.py
│   └── test_orders.py
└── src/
    └── futu_opend_execution/
        ├── __init__.py
        ├── config.py
        ├── models.py
        ├── risk.py
        ├── execution/
        │   ├── __init__.py
        │   ├── broker.py
        │   ├── futu.py
        │   └── simulator.py
        └── services/
            ├── __init__.py
            ├── greymarket.py
            └── orders.py
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

## Grey-Market Buy Planner

This repository now includes a simulation-first planner for a grey-market
"buy after open but avoid overpaying" workflow.

Current planner behavior:
- takes a snapshot of the visible ask book after grey-market open
- walks the ask ladder from low to high until the target quantity is covered
- uses the marginal ask as the minimum limit price required for a full visible fill
- optionally adds a tick buffer when queue priority matters more than price
- simulates the expected fill against the same snapshot before any broker wiring exists

This means the current feature answers:
"Given the visible asks right now, what is the lowest limit price that should
cover my target quantity?"

It does not yet guarantee a live fill because OpenD submission, latency,
queue-position changes, and disappearing liquidity are still outside the current
scope.

## Broker Submission

This repository now includes a broker-facing execution workflow and a Futu OpenD
adapter shape.

Current behavior:
- `submit_grey_market_buy_plan(...)` submits a limit buy plan, polls the order
  state, and returns a consolidated execution report
- `execute_grey_market_buy(...)` combines planning plus submission in one step
- `run_grey_market_snatch(...)` waits for a tradable market state, fetches the
  live order book, builds the plan, and optionally submits it
- `FutuOpenDTradeBroker` maps the workflow to Futu's trading context methods for
  place-order, query-order, history-order lookup, cancel, and unlock
- `FutuOpenDQuoteClient` maps market-state and order-book reads to Futu quote
  APIs
- `IOC` is emulated as a `DAY` order plus cancel-on-timeout because the current
  Futu stock order flow documented for Python exposes `DAY` and `GTC`, but not a
  native `IOC`

The Futu SDK does not expose a dedicated "grey market open" enum in its
`MarketState` constants. The harness therefore waits for a configurable set of
"tradable enough" market states instead of a single hard-coded grey-market
value.

This means the current system can express:
"Place the lowest-price limit order that should cover my target quantity, then
cancel the remainder quickly if the order does not complete."

### Optional broker dependency

The OpenD adapter keeps `futu-api` optional so planning and tests still work
without the broker SDK.

```bash
pip install -e '.[futu]'
```

### Example

```python
from futu_opend_execution import (
    GreyMarketBuyRequest,
    OrderBookSnapshot,
    QuoteLevel,
    build_grey_market_buy_plan,
)

snapshot = OrderBookSnapshot(
    symbol="09868",
    asks=(
        QuoteLevel(price="3.28", quantity=100),
        QuoteLevel(price="3.29", quantity=200),
        QuoteLevel(price="3.30", quantity=500),
    ),
)

request = GreyMarketBuyRequest(
    symbol="09868",
    quantity=250,
    tick_size="0.01",
    price_buffer_ticks=0,
)

plan = build_grey_market_buy_plan(request, snapshot)
print(plan.minimum_limit_price)         # 3.29
print(plan.selected_limit_price)        # 3.29
print(plan.expected_fill.average_price) # 3.286
```

### Submission example

```python
from futu_opend_execution import (
    FutuOpenDTradeBroker,
    GreyMarketBuyRequest,
    OrderBookSnapshot,
    QuoteLevel,
    RuntimeConfig,
    execute_grey_market_buy,
)

config = RuntimeConfig.from_env()
request = GreyMarketBuyRequest(
    symbol="09868",
    quantity=250,
    ioc_timeout_seconds="0.8",
    remark="grey-open-snatch",
)
snapshot = OrderBookSnapshot(
    symbol="09868",
    asks=(
        QuoteLevel(price="3.28", quantity=100),
        QuoteLevel(price="3.29", quantity=200),
    ),
)

with FutuOpenDTradeBroker(config) as broker:
    report = execute_grey_market_buy(request, snapshot, broker, config=config)

print(report.latest_order.status)
print(report.latest_order.dealt_quantity)
print(report.remaining_quantity)
```

### CLI harness

```bash
PYTHONPATH=src python -m futu_opend_execution.harness 09868 250
PYTHONPATH=src python -m futu_opend_execution.harness 09868 250 --execute --remark grey-open-snatch
```

## Quick start

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -e .
```

If your Python build does not bundle `setuptools` or `wheel`, install those
packaging tools first before running the editable install.

### 2. Configure environment

```bash
cp .env.example .env
```

Default variables:

```env
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
FUTU_ALLOW_REAL_TRADE=0
FUTU_SECURITY_FIRM=FUTUSECURITIES
FUTU_ACC_ID=0
FUTU_ACC_INDEX=0
FUTU_TRADE_PASSWORD=
FUTU_SDK_HOME_OVERRIDE=
FUTU_ORDER_POLL_INTERVAL_SECONDS=0.2
FUTU_CANCEL_ORDER_GRACE_SECONDS=2.0
FUTU_DEFAULT_IOC_TIMEOUT_SECONDS=1.0
FUTU_QUOTE_POLL_INTERVAL_SECONDS=0.5
FUTU_DEFAULT_WAIT_FOR_OPEN_TIMEOUT_SECONDS=300.0
FUTU_DEFAULT_ORDER_BOOK_DEPTH=10
FUTU_GREY_MARKET_OPEN_STATES=AUCTION,MORNING,AFTERNOON,AFTER_HOURS_BEGIN,HK_CAS,NIGHT_OPEN
```

### 3. Run local checks

```bash
python -m pip install --upgrade pip setuptools wheel
pip install -e .
python -c "import futu_opend_execution; print('ok')"
python -m unittest discover -s tests
```

## Development notes

- Requires Python 3.11+
- Assumes a local Futu OpenD instance is available when broker integration begins
- Grey-market planning is currently snapshot-driven and simulation-first
- The Futu broker adapter requires a logged-in OpenD instance plus the optional
  `futu-api` package
- When the SDK needs a writable HOME for its own log files, set
  `FUTU_SDK_HOME_OVERRIDE` to a writable directory before importing `futu`
- Real trading still requires explicit opt-in via `FUTU_ALLOW_REAL_TRADE=1`
- The Futu path emulates `IOC` using `DAY` plus fast cancel

## Roadmap

### Phase 1: scaffold
- [x] create package skeleton
- [x] publish repository
- [x] add CI and basic docs

### Phase 2: simulated execution MVP
- [x] config loader
- [ ] connection health check
- [x] execution request model
- [x] visible-book simulation for grey-market buy planning
- [ ] structured logging
- [x] grey-market order submitter wired to an OpenD adapter shape

### Phase 3: broker integration
- [x] OpenD trading context wrapper
- [x] order placement abstraction
- [x] order status reconciliation
- [ ] retry / timeout / reconnect policy
- [ ] explicit real-trade guardrails

## License

MIT
