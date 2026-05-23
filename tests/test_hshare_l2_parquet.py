from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.data.hshare_l2 import HshareL2ReplayProvider


@unittest.skipUnless(importlib.util.find_spec("polars"), "polars is required for parquet fixture")
class HshareL2ParquetTests(unittest.TestCase):
    def test_provider_maps_candidate_cleaned_rows(self) -> None:
        import polars as pl

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trades = root / "trades" / "date=2026-05-21"
            orders = root / "orders" / "date=2026-05-21"
            trades.mkdir(parents=True)
            orders.mkdir(parents=True)
            pl.DataFrame(
                {
                    "SendTime": ["2026-05-21T01:30:00+00:00"],
                    "Price": [100.0],
                    "Volume": [100],
                    "Dir": [1],
                    "source_file": ["trade/00700.csv"],
                }
            ).write_parquet(trades / "trades.parquet")
            pl.DataFrame(
                {
                    "SendTime": ["2026-05-21T01:30:00+00:00"],
                    "Price": [99.9],
                    "Volume": [100],
                    "Side": ["BUY"],
                    "source_file": ["order/00700.csv"],
                }
            ).write_parquet(orders / "orders.parquet")

            provider = HshareL2ReplayProvider(data_root=root, dates=["2026-05-21"], symbols=["HK.00700"])

            events = list(provider.iter_events())

            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].symbol, "HK.00700")


if __name__ == "__main__":
    unittest.main()
