from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from futu_opend_execution.data.market import MarketState
from futu_opend_execution.risk_sentinel import BlackSwanSentinel, RiskAction, RiskCategory, RiskSeverity
from futu_opend_execution.watchlist import WatchSymbolConfig


def _config() -> WatchSymbolConfig:
    return WatchSymbolConfig.from_dict(
        {
            "symbol": "HK.00700",
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
        }
    )


def _state(**overrides) -> MarketState:
    base = dict(
        symbol="HK.00700",
        timestamp=datetime(2026, 5, 21, 9, 30),
        interval_seconds=1,
        last_price=Decimal("100"),
        best_bid=Decimal("99.9"),
        bid_size=Decimal("1000"),
        best_ask=Decimal("100.0"),
        ask_size=Decimal("1000"),
        spread_bps=Decimal("10"),
        orderbook_imbalance=Decimal("0"),
        opening_vwap=Decimal("100"),
        rolling_vwap=Decimal("100"),
        realized_vol=Decimal("1"),
        rolling_high=Decimal("101"),
        rolling_low=Decimal("99"),
        cumulative_volume=Decimal("10000"),
        cumulative_turnover=Decimal("1000000"),
        volume_delta=Decimal("100"),
        turnover_delta=Decimal("10000"),
        tick_count=10,
        source="fixture",
        previous_close=Decimal("100"),
        open_price=Decimal("100"),
    )
    base.update(overrides)
    return MarketState(**base)


class BlackSwanSentinelTests(unittest.TestCase):
    def test_stale_data_pauses_trading(self) -> None:
        market = _state(stale=True)
        event = BlackSwanSentinel().evaluate(market=market, config=_config(), now=market.timestamp)[0]

        self.assertEqual(event.category, RiskCategory.DATA_STALE)
        self.assertEqual(event.action, RiskAction.PAUSE_TRADING)

    def test_price_crash_requires_manual_review(self) -> None:
        market = _state(last_price=Decimal("94"), rolling_high=Decimal("101"))
        events = BlackSwanSentinel().evaluate(market=market, config=_config(), now=market.timestamp)

        event = next(item for item in events if item.category is RiskCategory.PRICE_CRASH)
        self.assertEqual(event.severity, RiskSeverity.CRITICAL)
        self.assertEqual(event.action, RiskAction.REQUIRE_MANUAL_REVIEW)

    def test_gap_down_requires_manual_review(self) -> None:
        market = _state(open_price=Decimal("90"), last_price=Decimal("91"))
        events = BlackSwanSentinel().evaluate(market=market, config=_config(), now=market.timestamp)

        self.assertTrue(any(item.category is RiskCategory.GAP_DOWN for item in events))

    def test_spread_widen_pauses_trading(self) -> None:
        market = _state(spread_bps=Decimal("60"))
        events = BlackSwanSentinel().evaluate(market=market, config=_config(), now=market.timestamp)

        self.assertTrue(any(item.category is RiskCategory.SPREAD_WIDEN and item.action is RiskAction.PAUSE_TRADING for item in events))

    def test_liquidity_vanish_pauses_trading(self) -> None:
        market = _state(bid_size=Decimal("100"))
        events = BlackSwanSentinel().evaluate(market=market, config=_config(), now=market.timestamp)

        self.assertTrue(any(item.category is RiskCategory.LIQUIDITY_VANISH for item in events))

    def test_age_based_stale_detection(self) -> None:
        market = _state(timestamp=datetime(2026, 5, 21, 9, 30))
        events = BlackSwanSentinel().evaluate(market=market, config=_config(), now=market.timestamp + timedelta(seconds=4))

        self.assertTrue(any(item.category is RiskCategory.DATA_STALE for item in events))


if __name__ == "__main__":
    unittest.main()
