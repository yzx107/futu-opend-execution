# OpenD API Notes for PR1 / PR2

This note was checked against Futu API Doc v10.6 on 2026-05-24.

Official references:

- Futu API / OpenD introduction: https://openapi.futunn.com/futu-api-doc/en/intro/intro.html
- OpenD configuration and listening address: https://openapi.futunn.com/futu-api-doc/en/quick/opend-base.html
- Real-time order book: https://openapi.futunn.com/futu-api-doc/en/quote/get-order-book.html
- Market snapshot: https://openapi.futunn.com/futu-api-doc/en/quote/get-market-snapshot.html
- Place orders, PR2 background only: https://openapi.futunn.com/futu-api-doc/en/trade/place-order.html
- Modify or cancel orders: https://openapi.futunn.com/futu-api-doc/en/trade/modify-order.html
- Get open orders: https://openapi.futunn.com/futu-api-doc/en/trade/get-order-list.html
- Get historical orders: https://openapi.futunn.com/futu-api-doc/en/trade/get-history-order-list.html
- Unlock trade: https://openapi.futunn.com/futu-api-doc/en/trade/unlock.html
- Trade API overview / callbacks: https://openapi.futunn.com/futu-api-doc/en/trade/overview.html

## OpenD host and port

OpenD is the local or server-side gateway program for Futu API. The SDK connects to OpenD over TCP, and OpenD forwards requests to Futu servers and returns processed data. The OpenD UI exposes an API listening IP and port. The official OpenD config page lists `127.0.0.1` for local connections, `0.0.0.0` for all network cards, or a specific network-card address.

This repository requires OpenD access to stay on loopback by default. That is stricter than the quote API technically requires, but it matches this repo's safety model: the same OpenD process can also expose trade APIs, and PR1 must avoid accidental remote control surfaces. `RuntimeConfig.futu_host` defaults to `127.0.0.1`, and live quote providers reject non-loopback hosts.

## OpenQuoteContext

`OpenQuoteContext(host='127.0.0.1', port=11111)` is the official Python SDK quote context used to request quote data from OpenD. PR1 uses it only for read-only market data. Strategy modules must not import the Futu SDK directly.

## subscribe

The official order-book example subscribes before calling `get_order_book`:

```python
ret_sub = quote_ctx.subscribe(['US.AAPL'], [SubType.ORDER_BOOK], subscribe_push=False)[0]
```

The note in the official example says OpenD keeps receiving pushes from the server after subscription; `subscribe_push=False` means the script does not need push callbacks temporarily. PR1 subscribes `QUOTE` and `ORDER_BOOK` before polling snapshots/order books.

## get_order_book

Official signature:

```python
get_order_book(code, num=10)
```

The successful Python return is `(ret, data)`, where `data` is a dict. The documented order-book payload includes:

- `code`
- `name`
- `svr_recv_time_bid`
- `svr_recv_time_ask`
- `Bid`
- `Ask`

`Bid` and `Ask` are lists of tuples shaped like:

```python
(price, volume, order_num, order_details)
```

`order_details` can include exchange order IDs and volumes when the quote authority supports that level of detail. PR1 uses only top-level best price/volume and aggregate bid/ask sizes. It does not infer hidden order semantics.

## get_market_snapshot

Official signature:

```python
get_market_snapshot(code_list)
```

The successful return is `(ret, data)`, where `data` is a pandas DataFrame. The official docs say up to 400 symbols can be requested at once. Fields used by PR1 when present include:

- `code`
- `update_time`
- `last_price`
- `open_price`
- `prev_close_price`
- `volume`
- `turnover`
- `lot_size`
- `ask_price`
- `bid_price`
- `ask_vol`
- `bid_vol`
- `sec_status`

PR1 treats missing snapshot fields conservatively. It does not fabricate spread, imbalance, or freshness when the provider lacks enough data.

## Freshness and sync/async caveats

The Python quote request methods are called synchronously from PR1's monitor loop, but Futu's own trade docs note that Python APIs can be synchronous while network transport and callbacks are asynchronous. PR1 does not rely on callback ordering. The live provider maintains a local rolling buffer per symbol from repeated polls and marks data stale when snapshot timestamps are too old. The `BlackSwanSentinel` independently checks stale age against per-symbol watchlist thresholds.

The order-book docs note that server receive times can sometimes be zero, for example after server reboot or first cached data push. PR1 therefore prefers snapshot `update_time` for the main `MarketState.timestamp` and records limited/unavailable book state instead of pretending to know freshness.

## PR2 trading APIs

PR2 uses the official `futu-api` Python SDK through `FutuOpenDTradeBroker`. It does not implement raw TCP or protobuf calls.

### place_order

Official Python signature checked:

```python
place_order(
    price,
    qty,
    code,
    trd_side,
    order_type=OrderType.NORMAL,
    adjust_limit=0,
    trd_env=TrdEnv.REAL,
    acc_id=0,
    acc_index=0,
    remark=None,
    time_in_force=TimeInForce.DAY,
    fill_outside_rth=False,
    aux_price=None,
    trail_type=None,
    trail_value=None,
    trail_spread=None,
    session=Session.NONE,
    jp_acc_type=SubAccType.JP_GENERAL,
    position_id=NONE,
)
```

The return is `(ret, data)`. If `ret == RET_OK`, `data` is a pandas DataFrame order list. Fields used by this repo include:

- `order_id`
- `code`
- `trd_side`
- `order_type`
- `order_status`
- `qty`
- `price`
- `create_time`
- `updated_time`
- `dealt_qty`
- `dealt_avg_price`
- `last_err_msg`
- `remark`
- `time_in_force`
- `currency`

PR2 submits only `OrderType.NORMAL` limit-style orders via `place_limit_buy` / `place_limit_sell`. It does not expose market orders, stop orders, trailing orders, or direct symbol/qty/price real-order CLI input.

### modify_order CANCEL

Official Python signature checked:

```python
modify_order(
    modify_order_op,
    order_id,
    qty,
    price,
    adjust_limit=0,
    trd_env=TrdEnv.REAL,
    acc_id=0,
    acc_index=0,
    aux_price=None,
    trail_type=None,
    trail_value=None,
    trail_spread=None,
)
```

For `ModifyOrderOp.CANCEL`, the docs say the unfilled remaining quantity is cancelled and `qty` / `price` are ignored. PR2 uses this only for timeout cancellation after a guarded manual order has already been submitted.

### order_list_query

Official Python signature checked:

```python
order_list_query(
    order_id="",
    order_market=TrdMarket.NONE,
    status_filter_list=[],
    code="",
    start="",
    end="",
    trd_env=TrdEnv.REAL,
    acc_id=0,
    acc_index=0,
    refresh_cache=False,
)
```

It queries open orders for the specified account, including filled or cancelled orders within 24 hours. `refresh_cache=True` asks OpenD to refresh from server immediately. PR2's adapter queries by `order_id` and `code`, with `refresh_cache=True`, then falls back to historical order query if the current list does not contain the order.

### history_order_list_query

Official Python signature checked:

```python
history_order_list_query(
    status_filter_list=[],
    code="",
    order_market=TrdMarket.NONE,
    start="",
    end="",
    trd_env=TrdEnv.REAL,
    acc_id=0,
    acc_index=0,
)
```

It queries historical orders for the specified account. The docs strongly recommend `acc_id` over `acc_index` because account indexes can change when accounts are added or closed. PR2 keeps the existing `RuntimeConfig.futu_acc_id` preference.

### unlock_trade

Official Python signature checked:

```python
unlock_trade(password=None, password_md5=None, is_unlock=True)
```

The docs state that a password is required to unlock transactions; if `password_md5` is supplied, OpenD uses that, otherwise the SDK derives MD5 from `password`. The docs also state that live trading accounts need unlock before `Place Order` or `Modify or Cancel Orders`; paper trading accounts do not. PR2 keeps `unlock_trade` inside `FutuOpenDTradeBroker`, calls it only for `TradeMode.REAL`, requires `FUTU_TRADE_PASSWORD`, and never writes the password to audit logs.

### Sync/async caveat

The official place-order docs warn that the Python API call is synchronous while network transport and trade callbacks are asynchronous; an order or fill callback can arrive before `place_order` returns if timing is tight. PR2 does not rely on callback ordering. It writes the immediate broker response, then polls `order_list_query` / `history_order_list_query` snapshots and applies only confirmed cumulative `dealt_qty` deltas.

The Trade API overview lists order callbacks and deal callbacks. PR2 does not subscribe to callbacks yet; polling is the safer low-coupling path for this stacked PR. If callbacks are added later, the fill ledger must remain idempotent and must treat callback/poll races as normal.

## Local uncertainty

The official docs describe the SDK-level return contracts, but exact pandas dtypes, enum string values, and partial-fill/cancel timing can vary across installed `futu-api` versions and OpenD runtime state. PR2 unit tests use fake brokers only. Before any real-money use, run a manual OpenD paper/simulated check on the target machine, verify `order_status` strings and `dealt_qty` behavior, and confirm that account selection uses the intended `FUTU_ACC_ID`.
