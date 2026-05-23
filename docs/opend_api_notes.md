# OpenD API Notes for PR1

This note was checked against Futu API Doc v10.6 on 2026-05-23.

Official references:

- Futu API / OpenD introduction: https://openapi.futunn.com/futu-api-doc/en/intro/intro.html
- OpenD configuration and listening address: https://openapi.futunn.com/futu-api-doc/en/quick/opend-base.html
- Real-time order book: https://openapi.futunn.com/futu-api-doc/en/quote/get-order-book.html
- Market snapshot: https://openapi.futunn.com/futu-api-doc/en/quote/get-market-snapshot.html
- Place orders, PR2 background only: https://openapi.futunn.com/futu-api-doc/en/trade/place-order.html
- Modify or cancel orders, PR2 background only: https://openapi.futunn.com/futu-api-doc/en/trade/modify-order.html

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

## Trading APIs deferred to PR2

Official `place_order` signature includes live and paper trading parameters such as price, quantity, code, side, order type, trade environment, account ID/index, time-in-force, and session. Official `modify_order` can cancel or modify existing orders. PR1 does not call either API and adds no web button or CLI path that can submit real orders.

TODO for PR2: wire `place_order` / `modify_order` only behind explicit real-trade gates, local OpenD loopback, kill switch checks, manual approval, no market orders, limit-order-only semantics, and account-specific audit logging.

## Local uncertainty

The official docs describe the SDK-level return contracts, but exact pandas dtypes and enum string values can vary across installed `futu-api` versions and OpenD runtime state. PR1 adapters parse only documented field names, keep `futu-api` optional for tests, and require live OpenD behavior to be rechecked on the machine before PR2 real-order work.
