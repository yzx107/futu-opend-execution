"""Tests for MarketPressureCalculator and TradingPositionEngine."""

from __future__ import annotations

import unittest
from decimal import Decimal

from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState
from futu_opend_execution.signals.market_pressure import (
    MarketPressure,
    MarketPressureCalculator,
)
from futu_opend_execution.services.trading_position import (
    TradingAction,
    TradingDecision,
    TradingPhase,
    TradingPositionEngine,
    TradingPositionRules,
)


D = Decimal


def _market(
    *,
    last_price: Decimal = D("10.00"),
    opening_vwap: Decimal | None = D("10.00"),
    rolling_vwap: Decimal | None = D("10.00"),
    realized_vol: Decimal = D("0.20"),
    rolling_high: Decimal | None = D("10.30"),
    rolling_low: Decimal | None = D("9.70"),
    orderbook_imbalance: Decimal = D("0"),
    spread_bps: Decimal = D("30"),
    tick_count: int = 20,
    volume_delta: Decimal = D("100"),
    cumulative_turnover: Decimal = D("100000"),
) -> AdaptiveMarketState:
    return AdaptiveMarketState(
        opening_vwap=opening_vwap,
        rolling_vwap=rolling_vwap,
        realized_vol=realized_vol,
        rolling_high=rolling_high,
        rolling_low=rolling_low,
        cumulative_turnover=cumulative_turnover,
        volume_delta=volume_delta,
        turnover_delta=D("0"),
        cumulative_field_reset_detected=False,
        tick_count=tick_count,
        orderbook_imbalance=orderbook_imbalance,
        spread_bps=spread_bps,
        last_price=last_price,
    )


class TestMarketPressureCalculator(unittest.TestCase):

    def test_data_insufficient_when_vol_zero(self):
        calc = MarketPressureCalculator()
        m = _market(realized_vol=D("0"), tick_count=5)
        p = calc.compute(m, elapsed_seconds=30)
        self.assertFalse(p.data_sufficient)

    def test_data_insufficient_when_few_ticks(self):
        calc = MarketPressureCalculator(min_ticks_for_data=10)
        m = _market(tick_count=5)
        p = calc.compute(m, elapsed_seconds=30)
        self.assertFalse(p.data_sufficient)

    def test_data_sufficient(self):
        calc = MarketPressureCalculator()
        m = _market(tick_count=20, realized_vol=D("0.2"))
        p = calc.compute(m, elapsed_seconds=30)
        self.assertTrue(p.data_sufficient)

    def test_price_z_score_above_anchor(self):
        calc = MarketPressureCalculator()
        # price=10.40, anchor=10.00, vol=0.20 → z = (10.40 - 10.00)/0.20 = 2.0
        m = _market(last_price=D("10.40"), opening_vwap=D("10.00"), realized_vol=D("0.20"))
        p = calc.compute(m, elapsed_seconds=60)
        self.assertAlmostEqual(float(p.price_z_score), 2.0, places=2)

    def test_price_z_score_below_anchor(self):
        calc = MarketPressureCalculator()
        # price=9.60, anchor=10.00, vol=0.20 → z = -2.0
        m = _market(last_price=D("9.60"), opening_vwap=D("10.00"), realized_vol=D("0.20"))
        p = calc.compute(m, elapsed_seconds=60)
        self.assertAlmostEqual(float(p.price_z_score), -2.0, places=2)

    def test_time_pressure_zero_at_start(self):
        calc = MarketPressureCalculator()
        m = _market()
        p = calc.compute(m, elapsed_seconds=0)
        self.assertEqual(p.time_pressure, D("0"))

    def test_time_pressure_one_at_end(self):
        calc = MarketPressureCalculator()
        m = _market()
        p = calc.compute(m, elapsed_seconds=9000)
        self.assertEqual(p.time_pressure, D("1"))

    def test_liquidity_score_tight_spread(self):
        calc = MarketPressureCalculator()
        m = _market(spread_bps=D("10"))
        p = calc.compute(m, elapsed_seconds=60)
        self.assertGreater(float(p.liquidity_score), 0.9)

    def test_liquidity_score_wide_spread(self):
        calc = MarketPressureCalculator()
        m = _market(spread_bps=D("200"))
        p = calc.compute(m, elapsed_seconds=60)
        self.assertAlmostEqual(float(p.liquidity_score), 0.0, places=2)


class TestTradingPositionEngine(unittest.TestCase):

    def _engine(self, qty: int = 500, lot_size: int = 100, **kw) -> TradingPositionEngine:
        rules = TradingPositionRules(**kw)
        return TradingPositionEngine(rules, total_trading_qty=qty, lot_size=lot_size)

    def _sufficient_pressure(self, **overrides) -> MarketPressure:
        defaults = dict(
            price_z_score=D("0.5"),
            momentum_score=D("0"),
            orderbook_pressure=D("0"),
            volume_intensity=D("1"),
            liquidity_score=D("0.8"),
            time_pressure=D("0.1"),
            data_sufficient=True,
        )
        defaults.update(overrides)
        return MarketPressure(**defaults)

    def _insufficient_pressure(self) -> MarketPressure:
        return MarketPressure(
            price_z_score=D("0"),
            momentum_score=D("0"),
            orderbook_pressure=D("0"),
            volume_intensity=D("0"),
            liquidity_score=D("0"),
            time_pressure=D("0"),
            data_sufficient=False,
        )

    # --- Phase transitions ---

    def test_starts_in_waiting_data(self):
        eng = self._engine()
        self.assertEqual(eng.phase, TradingPhase.WAITING_DATA)

    def test_transitions_to_ready_on_sufficient_data(self):
        eng = self._engine()
        m = _market()
        p = self._sufficient_pressure()
        eng.evaluate(m, p)
        self.assertEqual(eng.phase, TradingPhase.READY_TO_BUY)

    def test_stays_waiting_when_data_insufficient(self):
        eng = self._engine()
        m = _market(realized_vol=D("0"))
        p = self._insufficient_pressure()
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.WAIT)
        self.assertEqual(eng.phase, TradingPhase.WAITING_DATA)

    # --- Buy logic ---

    def test_buy_on_pullback(self):
        eng = self._engine()
        m = _market(last_price=D("10.10"), opening_vwap=D("10.00"), realized_vol=D("0.20"))
        # First tick: transition out of WAITING_DATA
        p = self._sufficient_pressure(
            price_z_score=D("0.5"),
            momentum_score=D("-0.6"),  # pullback
        )
        eng.evaluate(m, p)
        # Second tick: should see BUY signal
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.BUY_TRADING)
        self.assertIn("pullback", d.reason)

    def test_buy_on_bid_pressure(self):
        eng = self._engine()
        m = _market()
        p_init = self._sufficient_pressure()
        eng.evaluate(m, p_init)  # transition
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.BUY_TRADING)
        self.assertIn("bid_pressure", d.reason)

    def test_buy_on_stable_anchor(self):
        eng = self._engine()
        m = _market()
        p_init = self._sufficient_pressure()
        eng.evaluate(m, p_init)  # transition
        p = self._sufficient_pressure(
            price_z_score=D("0.3"),
            volume_intensity=D("0.3"),
        )
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.BUY_TRADING)
        self.assertIn("stable", d.reason)

    def test_no_buy_when_price_too_high(self):
        eng = self._engine()
        m = _market()
        p_init = self._sufficient_pressure()
        eng.evaluate(m, p_init)
        p = self._sufficient_pressure(price_z_score=D("3.0"))
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.WAIT)

    def test_buy_time_urgency(self):
        eng = self._engine(buy_time_urgency=D("0.1"))
        m = _market()
        p_init = self._sufficient_pressure()
        eng.evaluate(m, p_init)  # transition
        p = self._sufficient_pressure(time_pressure=D("0.2"))
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.BUY_TRADING)
        self.assertIn("time_urgency", d.reason)

    def test_tranche_quantity_splits(self):
        eng = self._engine(qty=1000, lot_size=100, tranche_count=2)
        m = _market()
        p_init = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p_init)  # transition
        d = eng.evaluate(m, p_init)
        self.assertEqual(d.action, TradingAction.BUY_TRADING)
        self.assertEqual(d.quantity, 500)  # first tranche = 1000 / 2

    # --- Confirm fills ---

    def test_confirm_buy_transitions_to_holding(self):
        eng = self._engine(qty=500, lot_size=100, tranche_count=1)
        m = _market()
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p)  # transition
        d = eng.evaluate(m, p)
        self.assertEqual(d.action, TradingAction.BUY_TRADING)
        eng.confirm_buy_fill(500, D("10.00"))
        self.assertEqual(eng.phase, TradingPhase.HOLDING)

    # --- Exhaustion sell ---

    def test_exhaustion_sell(self):
        eng = self._engine(qty=500, lot_size=100, tranche_count=1)
        m = _market()
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p)
        eng.evaluate(m, p)
        eng.confirm_buy_fill(500, D("10.00"))

        # Now simulate exhaustion conditions.
        p_exh = self._sufficient_pressure(
            price_z_score=D("2.0"),
            momentum_score=D("-0.5"),
            orderbook_pressure=D("-0.2"),
        )
        d = eng.evaluate(m, p_exh)
        self.assertEqual(d.action, TradingAction.SELL_TRADING)
        self.assertIn("exhaustion", d.reason)

    # --- Panic sell ---

    def test_panic_sell(self):
        eng = self._engine(qty=500, lot_size=100, tranche_count=1)
        m = _market()
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p)
        eng.evaluate(m, p)
        eng.confirm_buy_fill(500, D("10.00"))

        m_panic = _market(
            last_price=D("9.60"),
            rolling_low=D("9.60"),
        )
        p_panic = self._sufficient_pressure(
            price_z_score=D("-2.0"),
            volume_intensity=D("2.0"),
            orderbook_pressure=D("-0.5"),
        )
        d = eng.evaluate(m_panic, p_panic)
        self.assertEqual(d.action, TradingAction.SELL_TRADING)
        self.assertIn("panic", d.reason)
        self.assertEqual(eng.phase, TradingPhase.EXITED)

    def test_panic_sells_core_when_configured(self):
        eng = self._engine(qty=500, lot_size=100, tranche_count=1, panic_sells_core=True)
        m = _market()
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p)
        eng.evaluate(m, p)
        eng.confirm_buy_fill(500, D("10.00"))

        m_panic = _market(last_price=D("9.60"), rolling_low=D("9.60"))
        p_panic = self._sufficient_pressure(
            price_z_score=D("-2.0"),
            volume_intensity=D("2.0"),
            orderbook_pressure=D("-0.5"),
        )
        d = eng.evaluate(m_panic, p_panic)
        self.assertEqual(d.action, TradingAction.SELL_ALL)

    # --- Close-of-session ---

    def test_close_sell_when_thin_profit(self):
        eng = self._engine(qty=500, lot_size=100, tranche_count=1)
        m = _market()
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p)
        eng.evaluate(m, p)
        eng.confirm_buy_fill(500, D("10.00"))

        # Time is 90% through session, small profit.
        m_close = _market(last_price=D("10.05"), realized_vol=D("0.20"))
        p_close = self._sufficient_pressure(
            price_z_score=D("0.25"),
            time_pressure=D("0.90"),
        )
        d = eng.evaluate(m_close, p_close)
        self.assertEqual(d.action, TradingAction.SELL_TRADING)
        self.assertIn("close", d.reason)

    def test_close_hold_when_large_profit(self):
        eng = self._engine(qty=500, lot_size=100, tranche_count=1)
        m = _market()
        p = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        eng.evaluate(m, p)
        eng.evaluate(m, p)
        eng.confirm_buy_fill(500, D("10.00"))

        # Large profit: pnl/share=1.0, overnight_risk=2*0.2=0.4, rr=2.5
        # z-score kept at 1.0 (below exhaustion_z=1.5) with positive
        # momentum so the exhaustion detector does *not* fire first.
        m_close = _market(last_price=D("11.00"), realized_vol=D("0.20"))
        p_close = self._sufficient_pressure(
            price_z_score=D("1.0"),
            momentum_score=D("0.5"),
            time_pressure=D("0.90"),
        )
        d = eng.evaluate(m_close, p_close)
        self.assertEqual(d.action, TradingAction.HOLD)
        self.assertIn("close_hold", d.reason)

    # --- Full lifecycle ---

    def test_full_lifecycle_buy_then_exhaustion_sell(self):
        eng = self._engine(qty=200, lot_size=100, tranche_count=1)
        m = _market()
        # 1. Wait for data
        p_init = self._sufficient_pressure(orderbook_pressure=D("0.4"))
        d1 = eng.evaluate(m, p_init)
        self.assertEqual(eng.phase, TradingPhase.READY_TO_BUY)

        # 2. Buy signal fires
        d2 = eng.evaluate(m, p_init)
        self.assertEqual(d2.action, TradingAction.BUY_TRADING)
        eng.confirm_buy_fill(200, D("10.00"))
        self.assertEqual(eng.phase, TradingPhase.HOLDING)

        # 3. Exhaustion sell
        p_exh = self._sufficient_pressure(
            price_z_score=D("2.0"),
            momentum_score=D("-0.5"),
            orderbook_pressure=D("-0.2"),
        )
        d3 = eng.evaluate(m, p_exh)
        self.assertEqual(d3.action, TradingAction.SELL_TRADING)
        eng.confirm_sell_fill(d3.quantity, D("10.40"))

        # 4. If fully sold, should be EXITED
        if eng.state.qty_held <= 0:
            d4 = eng.evaluate(m, p_exh)
            self.assertEqual(eng.phase, TradingPhase.EXITED)


if __name__ == "__main__":
    unittest.main()
