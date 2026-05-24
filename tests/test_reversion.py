from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from futu_opend_execution.agent.reversion import (
    SellRebuyGrid,
    SellRebuyRules,
    evaluate_sell_rebuy,
    evaluate_newly_listed_sell_rebuy,
)
from futu_opend_execution.data.market import MarketState


class SellRebuyReversionTests(unittest.TestCase):
    def test_sell_rebuy_round_trip_uses_realized_net_pnl(self) -> None:
        states = [
            _state(0, "100"),
            _state(1, "102"),
            _state(2, "100.3"),
        ]

        result = evaluate_sell_rebuy(
            states,
            SellRebuyRules(
                entry_vol_multiple=Decimal("1.5"),
                exit_vol_band=Decimal("0.5"),
                cost_bps=Decimal("35"),
            ),
        )

        self.assertEqual(result["sell_count"], 1)
        self.assertEqual(result["round_trips_completed"], 1)
        self.assertEqual(result["open_quantity"], 0)
        self.assertGreater(Decimal(result["net_pnl_after_cost"]), Decimal("0"))

    def test_forced_rebuy_counts_as_risk(self) -> None:
        states = [_state(0, "100"), _state(1, "102"), _state(2, "104")]

        result = evaluate_sell_rebuy(
            states,
            SellRebuyRules(
                entry_vol_multiple=Decimal("1.5"),
                exit_vol_band=Decimal("0"),
                stop_vol_multiple=Decimal("1.0"),
                cost_bps=Decimal("35"),
            ),
        )

        self.assertEqual(result["forced_rebuy_count"], 1)
        self.assertLess(Decimal(result["net_pnl_after_cost"]), Decimal("0"))

    def test_buy_sell_direction_can_trade_dip_rebound(self) -> None:
        states = [_state(0, "100"), _state(1, "98"), _state(2, "100.5")]

        result = evaluate_sell_rebuy(
            states,
            SellRebuyRules(
                direction="BUY_SELL",
                entry_vol_multiple=Decimal("1.5"),
                exit_vol_band=Decimal("0.5"),
                cost_bps=Decimal("35"),
            ),
        )

        self.assertEqual(result["round_trips_completed"], 1)
        self.assertEqual(result["buy_count"], 1)
        self.assertEqual(result["rebuy_count"], 0)
        self.assertGreater(Decimal(result["net_pnl_after_cost"]), Decimal("0"))

    def test_newly_listed_sell_rebuy_summary_can_mark_candidate_with_fake_market_states(self) -> None:
        def fake_market_states(symbol, trade_date, data_root, top_of_book_root):
            return [_state(0, "100"), _state(1, "102"), _state(2, "100.3")]

        import futu_opend_execution.agent.reversion as reversion

        original_universe = reversion.build_newly_listed_universe
        original_market_states = reversion._market_states
        try:
            reversion.build_newly_listed_universe = lambda **_: {
                "candidate_count": 1,
                "candidates": [
                    {"symbol": "HK.01609", "listing_date": "2026-05-05", "available_trade_dates": ["2026-05-20", "2026-05-22"]}
                ],
            }
            reversion._market_states = fake_market_states
            summary = evaluate_newly_listed_sell_rebuy(
                validation_days=1,
                min_validation_round_trips=1,
                max_quality_block_ratio="1",
                grid=SellRebuyGrid(
                    entry_vol_multiple=(Decimal("1.5"),),
                    direction=("SELL_REBUY",),
                    exit_vol_band=(Decimal("0.5"),),
                    stop_vol_multiple=(Decimal("3"),),
                    max_hold_states=(60,),
                    cost_bps=(Decimal("35"),),
                ),
            )
        finally:
            reversion.build_newly_listed_universe = original_universe
            reversion._market_states = original_market_states

        self.assertEqual(summary["decision"], "CANDIDATE")
        self.assertEqual(summary["candidate_count"], 1)
        self.assertGreater(Decimal(summary["recommended_candidate"]["validation"]["net_pnl_after_cost_sum"]), Decimal("0"))

    def test_validation_winner_is_blocked_when_train_loses_money(self) -> None:
        def fake_market_states(symbol, trade_date, data_root, top_of_book_root):
            if trade_date == "2026-05-20":
                return [_state(0, "100"), _state(1, "102"), _state(2, "104")]
            return [_state(0, "100"), _state(1, "102"), _state(2, "100.3")]

        import futu_opend_execution.agent.reversion as reversion

        original_universe = reversion.build_newly_listed_universe
        original_market_states = reversion._market_states
        try:
            reversion.build_newly_listed_universe = lambda **_: {
                "candidate_count": 1,
                "candidates": [
                    {"symbol": "HK.01609", "listing_date": "2026-05-05", "available_trade_dates": ["2026-05-20", "2026-05-22"]}
                ],
            }
            reversion._market_states = fake_market_states
            summary = evaluate_newly_listed_sell_rebuy(
                validation_days=1,
                min_validation_round_trips=1,
                max_quality_block_ratio="1",
                grid=SellRebuyGrid(
                    entry_vol_multiple=(Decimal("1.5"),),
                    direction=("SELL_REBUY",),
                    exit_vol_band=(Decimal("0.5"),),
                    stop_vol_multiple=(Decimal("1"),),
                    max_hold_states=(60,),
                    cost_bps=(Decimal("35"),),
                ),
            )
        finally:
            reversion.build_newly_listed_universe = original_universe
            reversion._market_states = original_market_states

        self.assertEqual(summary["decision"], "NO_GO")
        self.assertEqual(summary["candidate_count"], 0)
        self.assertIn("train net_pnl_after_cost below threshold", summary["walk_forward_ranking"][0]["candidate_reasons"])


def _state(offset: int, price: str) -> MarketState:
    value = Decimal(price)
    return MarketState(
        symbol="HK.01609",
        timestamp=datetime(2026, 5, 22, 9, 30) + timedelta(seconds=offset),
        interval_seconds=1,
        last_price=value,
        best_bid=value - Decimal("0.01"),
        bid_size=Decimal("100"),
        best_ask=value + Decimal("0.01"),
        ask_size=Decimal("100"),
        spread_bps=Decimal("1"),
        orderbook_imbalance=Decimal("0"),
        opening_vwap=Decimal("100"),
        rolling_vwap=Decimal("100"),
        realized_vol=Decimal("1"),
        rolling_high=value,
        rolling_low=value,
        cumulative_volume=Decimal("100"),
        cumulative_turnover=value * Decimal("100"),
        volume_delta=Decimal("100"),
        turnover_delta=value * Decimal("100"),
        tick_count=10,
        source="fixture",
        book_quality="OK",
    )


if __name__ == "__main__":
    unittest.main()
