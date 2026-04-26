"""CLI harness for the grey-market snatch workflow."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, ROUND_DOWN

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
        "--tranche-weights",
        default=None,
        help=(
            "Optional comma-separated weights to split quantity into staged tranches, "
            "for example: 0.5,0.3,0.2"
        ),
    )
    parser.add_argument(
        "--tranche-buffer-ticks",
        default=None,
        help=(
            "Optional comma-separated buffer ticks per tranche, for example: 0,1,2. "
            "Must match tranche count."
        ),
    )
    parser.add_argument(
        "--allow-partial-fill-final-tranche",
        action="store_true",
        help="Allow partial fill on final tranche only.",
    )
    parser.add_argument(
        "--remark",
        default=None,
        help="Optional broker order remark",
    )
    return parser


def parse_decimal_csv(value: str | None) -> tuple[Decimal, ...]:
    if value is None:
        return ()
    items = [item.strip() for item in str(value).split(",") if item.strip()]
    if not items:
        return ()
    parsed = tuple(Decimal(item) for item in items)
    if any(item <= 0 for item in parsed):
        raise ValueError("All tranche weights must be positive.")
    return parsed


def parse_int_csv(value: str | None) -> tuple[int, ...]:
    if value is None:
        return ()
    items = [item.strip() for item in str(value).split(",") if item.strip()]
    if not items:
        return ()
    parsed = tuple(int(item) for item in items)
    if any(item < 0 for item in parsed):
        raise ValueError("All tranche buffer ticks must be zero or positive.")
    return parsed


def build_tranche_quantities(total_quantity: int, weights: tuple[Decimal, ...]) -> tuple[int, ...]:
    if total_quantity <= 0:
        raise ValueError("total_quantity must be positive.")
    if not weights:
        return (total_quantity,)

    if total_quantity < len(weights):
        raise ValueError(
            "total_quantity is too small for the tranche count; each tranche must be at least 1 share."
        )

    total_weight = sum(weights)
    raw_allocations = [
        (Decimal(total_quantity) * weight) / total_weight
        for weight in weights
    ]
    base_quantities = [
        int(allocation.to_integral_value(rounding=ROUND_DOWN))
        for allocation in raw_allocations
    ]
    missing = total_quantity - sum(base_quantities)

    # Distribute remainder by largest fractional part first.
    fractional_rank = sorted(
        range(len(raw_allocations)),
        key=lambda idx: raw_allocations[idx] - Decimal(base_quantities[idx]),
        reverse=True,
    )
    for idx in fractional_rank[:missing]:
        base_quantities[idx] += 1

    if any(quantity <= 0 for quantity in base_quantities):
        raise ValueError(
            "Invalid tranche split generated zero quantity tranche; use fewer tranches or larger quantity."
        )

    return tuple(base_quantities)


def build_buffer_ticks(
    *,
    tranche_count: int,
    default_ticks: int,
    configured_ticks: tuple[int, ...],
) -> tuple[int, ...]:
    if configured_ticks:
        if len(configured_ticks) != tranche_count:
            raise ValueError("tranche-buffer-ticks must have the same length as tranche-weights.")
        return configured_ticks

    return tuple(default_ticks + index for index in range(tranche_count))


def _build_single_request(
    *,
    args,
    quantity: int,
    price_buffer_ticks: int,
    allow_partial_fill: bool,
    remark_suffix: str,
) -> GreyMarketBuyRequest:
    remark_base = args.remark or "harness_grey_snatch"
    return GreyMarketBuyRequest(
        symbol=args.symbol,
        quantity=quantity,
        tick_size=args.tick_size,
        price_buffer_ticks=price_buffer_ticks,
        max_limit_price=args.max_limit_price,
        allow_partial_fill=allow_partial_fill,
        ioc_timeout_seconds=args.ioc_timeout_seconds,
        remark=f"{remark_base}_{remark_suffix}",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = RuntimeConfig.from_env()

    try:
        weights = parse_decimal_csv(args.tranche_weights)
        tranche_quantities = build_tranche_quantities(args.quantity, weights)
        tranche_ticks = build_buffer_ticks(
            tranche_count=len(tranche_quantities),
            default_ticks=args.price_buffer_ticks,
            configured_ticks=parse_int_csv(args.tranche_buffer_ticks),
        )
    except (ArithmeticError, ValueError) as exc:
        raise SystemExit(f"Invalid tranche config: {exc}") from exc

    summaries: list[dict[str, object]] = []

    with FutuOpenDQuoteClient(config) as quote_client:
        if args.execute:
            with FutuOpenDTradeBroker(config) as broker:
                for index, quantity in enumerate(tranche_quantities, start=1):
                    request = _build_single_request(
                        args=args,
                        quantity=quantity,
                        price_buffer_ticks=tranche_ticks[index - 1],
                        allow_partial_fill=(
                            args.allow_partial_fill
                            or (
                                args.allow_partial_fill_final_tranche
                                and index == len(tranche_quantities)
                            )
                        ),
                        remark_suffix=f"t{index}",
                    )
                    report = run_grey_market_snatch(
                        request,
                        quote_client,
                        broker=broker,
                        config=config,
                        wait_timeout_seconds=args.wait_timeout_seconds,
                    )
                    summaries.append(_report_summary(index=index, request=request, report=report))
        else:
            for index, quantity in enumerate(tranche_quantities, start=1):
                request = _build_single_request(
                    args=args,
                    quantity=quantity,
                    price_buffer_ticks=tranche_ticks[index - 1],
                    allow_partial_fill=(
                        args.allow_partial_fill
                        or (
                            args.allow_partial_fill_final_tranche
                            and index == len(tranche_quantities)
                        )
                    ),
                    remark_suffix=f"t{index}",
                )
                report = run_grey_market_snatch(
                    request,
                    quote_client,
                    broker=None,
                    config=config,
                    wait_timeout_seconds=args.wait_timeout_seconds,
                )
                summaries.append(_report_summary(index=index, request=request, report=report))

    total_requested = sum(int(item["request_quantity"]) for item in summaries)
    total_expected = sum(int(item["expected_fill_quantity"]) for item in summaries)
    total_dealt = sum(int(item.get("dealt_quantity", 0)) for item in summaries)

    print(
        json.dumps(
            {
                "symbol": args.symbol,
                "tranche_count": len(summaries),
                "total_requested_quantity": total_requested,
                "total_expected_fill_quantity": total_expected,
                "total_dealt_quantity": total_dealt,
                "submitted": any(bool(item["submitted"]) for item in summaries),
                "tranches": summaries,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def _report_summary(*, index: int, request: GreyMarketBuyRequest, report) -> dict[str, object]:
    summary: dict[str, object] = {
        "tranche": index,
        "request_quantity": request.quantity,
        "price_buffer_ticks": request.price_buffer_ticks,
        "allow_partial_fill": request.allow_partial_fill,
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

    return summary


if __name__ == "__main__":
    raise SystemExit(main())
