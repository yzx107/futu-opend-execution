# Safety

The project is dry-run by default.

Real order submission must pass all server-side gates:

- `FUTU_ALLOW_REAL_TRADE=1`
- local OpenD host only
- confirmation phrase `确认实盘`
- kill switch absent
- explicit positive `max_qty` and `max_notional`
- lot-aligned positive quantity
- duplicate intent lock
- order rate limit
- valid market snapshot
- spread within configured limit
- valid inventory snapshot

The cost reducer may sell only `TRADING_SELL` inventory and may rebuy only previously sold trading inventory. It does not open new positions and does not sell core inventory. Market orders are intentionally absent.

`LIVE_REAL_COST_REDUCER_AUTO` remains experimental and disabled unless a local config explicitly enables it.
