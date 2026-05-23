from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.watchlist import WatchlistConfig, WatchlistConfigError, load_watchlist_config


def _valid_config() -> dict:
    return {
        "symbols": [
            {
                "symbol": "700",
                "enabled": True,
                "lot_size": 100,
                "current_qty": 2000,
                "cost_price": "300.0",
                "core_qty_target": 1400,
                "trading_qty_target": 600,
                "max_sell_qty_per_order": 100,
                "max_rebuy_qty_per_order": 100,
                "max_sell_total_position_ratio": "0.25",
                "max_round_trips": 2,
                "black_swan_thresholds": {
                    "intraday_drop_bps": "500",
                    "gap_down_bps": "800",
                    "spread_bps": "50",
                    "stale_seconds": "3",
                    "min_bid_size_lots": 3,
                },
                "cost_reducer_rules": {
                    "overextension_vol_multiple": "2.0",
                    "high_pullback_vol_multiple": "0.5",
                    "rebuy_anchor_vol_band": "1.0",
                    "max_spread_bps": "20",
                    "estimated_roundtrip_cost_bps": "35",
                    "safety_buffer_bps": "20",
                },
                "notes": "test",
            }
        ]
    }


class WatchlistConfigTests(unittest.TestCase):
    def test_valid_config_normalizes_symbol(self) -> None:
        config = WatchlistConfig.from_dict(_valid_config())

        self.assertEqual(config.symbols[0].symbol, "HK.00700")

    def test_load_valid_json_file(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "watchlist.json"
            path.write_text(json.dumps(_valid_config()), encoding="utf-8")

            self.assertEqual(load_watchlist_config(path).symbols[0].symbol, "HK.00700")

    def test_invalid_symbol_fails(self) -> None:
        data = _valid_config()
        data["symbols"][0]["symbol"] = "US.AAPL"

        with self.assertRaisesRegex(WatchlistConfigError, "symbol"):
            WatchlistConfig.from_dict(data)

    def test_invalid_lot_size_fails(self) -> None:
        data = _valid_config()
        data["symbols"][0]["lot_size"] = 0

        with self.assertRaisesRegex(WatchlistConfigError, "lot_size"):
            WatchlistConfig.from_dict(data)

    def test_current_qty_must_be_lot_aligned(self) -> None:
        data = _valid_config()
        data["symbols"][0]["current_qty"] = 2050
        data["symbols"][0]["core_qty_target"] = 1450

        with self.assertRaisesRegex(WatchlistConfigError, "current_qty"):
            WatchlistConfig.from_dict(data)

    def test_core_plus_trading_must_equal_current_qty(self) -> None:
        data = _valid_config()
        data["symbols"][0]["trading_qty_target"] = 500

        with self.assertRaisesRegex(WatchlistConfigError, "must equal current_qty"):
            WatchlistConfig.from_dict(data)

    def test_max_sell_ratio_bounds(self) -> None:
        data = copy.deepcopy(_valid_config())
        data["symbols"][0]["max_sell_total_position_ratio"] = "1.5"

        with self.assertRaisesRegex(WatchlistConfigError, "between 0 and 1"):
            WatchlistConfig.from_dict(data)


if __name__ == "__main__":
    unittest.main()
