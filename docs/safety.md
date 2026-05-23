# Safety

The project is dry-run by default.

PR2 adds a guarded manual real-order path, but it remains closed unless every gate below passes. There is no automatic real-money path and no `LIVE_REAL_AUTO` enablement.

Real order submission must pass all service-side gates:

- `FUTU_ALLOW_REAL_TRADE=1`
- local OpenD host only
- confirmation phrase `确认实盘`
- kill switch absent
- approval file present
- approval `approved=true`
- approval not expired
- approval snapshot matches the source signal when `signal_snapshot` is present
- no CRITICAL risk snapshot
- explicit positive `max_qty` and `max_notional`
- lot-aligned positive quantity
- limit price only
- duplicate intent lock
- order rate limit
- valid market snapshot
- spread within configured limit
- valid inventory snapshot

The cost reducer may sell only `TRADING_SELL` inventory and may rebuy only previously sold trading inventory. It does not open new positions and does not sell core inventory. Market orders are intentionally absent.

`LIVE_REAL_COST_REDUCER_AUTO` remains disabled by default and is not exposed by the PR2 CLI.

Manual real orders use `approvals/*.json` records. The example approval is intentionally expired and `approved=false`; it is a schema example, not an executable instruction. `validate-approval` performs static validation and never connects to OpenD. `submit-approved` still has to pass `RealOrderGuard` immediately before broker submission.

Order execution is limit-only and polls broker order status after submission. On timeout, the service sends `modify_order(CANCEL)` and reconciles only confirmed fills. Cancelled unfilled orders do not update inventory; cancelled partially filled orders update only the confirmed filled quantity. If a fill appears after cancellation, the audit log emits `reconciliation_warning` instead of silently ignoring it.

Paper and dry-run outputs are not promises of real fillability, slippage, liquidity, profitability, or final OpenD execution state. This PR is stacked on PR1 and depends on PR1's dry-run / risk / signal semantics while intentionally using dict snapshots to avoid duplicating PR1 models.
