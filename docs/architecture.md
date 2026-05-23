# Architecture

The agent is split by responsibility:

- `data`: adapters normalize replay/live inputs into `MarketEvent` and `MarketState`.
- `strategies`: strategy code depends only on market state and inventory snapshots.
- `agent`: runtime loops wire data, strategy, ledger, and guarded execution.
- `execution`: OpenD broker/position adapters and generic order-intent models.
- `ledger`: paper accounting for strategy validation.
- `cli` and `web`: operator interfaces.

The first strategy is `CostReducerStrategy`. It wraps the existing cost reducer state machine and keeps sell/rebuy decisions scoped to existing inventory. Future strategies should implement the same market-state input boundary and should not import OpenD SDK or parquet readers directly.
