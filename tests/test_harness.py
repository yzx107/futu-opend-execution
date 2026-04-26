from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from futu_opend_execution.harness import (
    build_buffer_ticks,
    build_tranche_quantities,
    main,
    parse_decimal_csv,
    parse_int_csv,
)


class HarnessTrancheTests(unittest.TestCase):
    def test_parse_csv_helpers(self) -> None:
        self.assertEqual(parse_decimal_csv("0.5, 0.3,0.2"), (Decimal("0.5"), Decimal("0.3"), Decimal("0.2")))
        self.assertEqual(parse_int_csv("0,1,2"), (0, 1, 2))

    def test_build_tranche_quantities_distributes_remainder(self) -> None:
        quantities = build_tranche_quantities(101, (Decimal("0.5"), Decimal("0.3"), Decimal("0.2")))
        self.assertEqual(sum(quantities), 101)
        self.assertEqual(quantities, (51, 30, 20))

    def test_build_buffer_ticks_defaults_to_ladder(self) -> None:
        ticks = build_buffer_ticks(tranche_count=3, default_ticks=1, configured_ticks=())
        self.assertEqual(ticks, (1, 2, 3))

    def test_main_runs_multi_tranche_dry_run(self) -> None:
        calls: list[tuple[int, int]] = []

        class FakeQuoteClient:
            def __init__(self, config) -> None:
                self.config = config

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        def fake_run_grey_market_snatch(request, quote_client, *, broker, config, wait_timeout_seconds):
            del quote_client, broker, config, wait_timeout_seconds
            calls.append((request.quantity, request.price_buffer_ticks))
            plan = SimpleNamespace(
                minimum_limit_price=Decimal("12.70"),
                selected_limit_price=Decimal("12.71"),
                expected_fill=SimpleNamespace(filled_quantity=request.quantity),
            )
            return SimpleNamespace(
                market_state="AFTER_HOURS_BEGIN",
                waited_seconds=Decimal("0.1"),
                plan=plan,
                submitted=False,
                execution_report=None,
            )

        stdout = io.StringIO()
        with patch("futu_opend_execution.harness.RuntimeConfig.from_env", return_value=SimpleNamespace()), patch(
            "futu_opend_execution.harness.FutuOpenDQuoteClient",
            FakeQuoteClient,
        ), patch(
            "futu_opend_execution.harness.run_grey_market_snatch",
            side_effect=fake_run_grey_market_snatch,
        ), redirect_stdout(stdout):
            rc = main(
                [
                    "HK.01234",
                    "100",
                    "--tranche-weights",
                    "0.5,0.3,0.2",
                    "--tranche-buffer-ticks",
                    "0,1,2",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [(50, 0), (30, 1), (20, 2)])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tranche_count"], 3)
        self.assertEqual(payload["total_requested_quantity"], 100)


if __name__ == "__main__":
    unittest.main()
