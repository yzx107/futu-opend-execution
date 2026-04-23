"""CLI harness for the grey-market snatch workflow."""

from __future__ import annotations

import argparse
import json

from futu_opend_execution import (
    FutuOpenDQuoteClient,
    FutuOpenDTradeBroker,
    GreyMarketBuyRequest,
    RuntimeConfig,
    run_grey_market_snatch,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the grey-market snatch workflow against Futu OpenD."
    )
    parser.add_argument("symbol", help="HK symbol, with or without HK. prefix")
    parser.add_argument("quantity", type=int, help="Target buy quantity")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit the order after planning instead of plan-only mode",
    )
    parser.add_argument(
        "--max-limit-price",
        dest="max_limit_price",
        help="Optional cap on the selected limit price",
    )
    parser.add_argument(
        "--tick-size",
        default="0.001",
        help="Tick size used when applying a price buffer",
    )
    parser.add_argument(
        "--price-buffer-ticks",
        type=int,
        default=0,
        help="How many ticks to add above the minimum visible fill price",
    )
    parser.add_argument(
        "--ioc-timeout-seconds",
        default=None,
        help="How long to wait before cancelling the remaining quantity",
    )
    parser.add_argument(
        "--wait-timeout-seconds",
        type=float,
        default=None,
        help="How long to wait for a tradable market state before aborting",
    )
    parser.add_argument(
        "--allow-partial-fill",
        action="store_true",
        help="Allow the order to continue even when visible liquidity is insufficient",
    )
    parser.add_argument(
        "--remark",
        default=None,
        help="Optional broker order remark",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = RuntimeConfig.from_env()
    request = GreyMarketBuyRequest(
        symbol=args.symbol,
        quantity=args.quantity,
        tick_size=args.tick_size,
        price_buffer_ticks=args.price_buffer_ticks,
        max_limit_price=args.max_limit_price,
        allow_partial_fill=args.allow_partial_fill,
        ioc_timeout_seconds=args.ioc_timeout_seconds,
        remark=args.remark,
    )

    with FutuOpenDQuoteClient(config) as quote_client:
        if args.execute:
            with FutuOpenDTradeBroker(config) as broker:
                report = run_grey_market_snatch(
                    request,
                    quote_client,
                    broker=broker,
                    config=config,
                    wait_timeout_seconds=args.wait_timeout_seconds,
                )
        else:
            report = run_grey_market_snatch(
                request,
                quote_client,
                broker=None,
                config=config,
                wait_timeout_seconds=args.wait_timeout_seconds,
            )

    summary = {
        "market_state": report.market_state,
        "waited_seconds": str(report.waited_seconds),
        "minimum_limit_price": str(report.plan.minimum_limit_price),
        "selected_limit_price": str(report.plan.selected_limit_price),
        "expected_fill_quantity": report.plan.expected_fill.filled_quantity,
        "submitted": report.submitted,
    }
    if report.execution_report is not None:
        summary["latest_order_status"] = report.execution_report.latest_order.status.value
        summary["dealt_quantity"] = report.execution_report.latest_order.dealt_quantity
        summary["remaining_quantity"] = report.execution_report.remaining_quantity
        summary["ioc_emulation_used"] = report.execution_report.ioc_emulation_used

    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
