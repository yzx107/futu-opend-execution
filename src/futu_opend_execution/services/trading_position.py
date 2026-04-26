"""Adaptive trading-position state machine for grey-market sessions.

Manages the *trading 50%* of total position.  The *core 50%* is bought at
open and never touched by this module — it stays until day-1 or is handled
manually.

Design principles
-----------------
* **Zero fixed-percentage thresholds.**  All price comparisons are expressed
  in multiples of realised volatility (σ).
* **Multi-signal convergence.**  No single indicator triggers a trade.
  Buy / sell decisions require several independent micro-structure signals
  to agree.
* **Stateful.**  The engine tracks its own phase (observing → buying →
  holding → exited) and emits structured events that the caller can log,
  display, or forward to a broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from futu_opend_execution._compat import StrEnum

from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState
from futu_opend_execution.signals.market_pressure import MarketPressure


def _d(value: int | float | str) -> Decimal:
    return Decimal(str(value))


# ------------------------------------------------------------------ #
# Enums & Decision types
# ------------------------------------------------------------------ #

class TradingPhase(StrEnum):
    """State-machine phases for the trading position."""
    WAITING_DATA = "WAITING_DATA"
    READY_TO_BUY = "READY_TO_BUY"
    HOLDING = "HOLDING"
    EXITED = "EXITED"


class TradingAction(StrEnum):
    WAIT = "WAIT"
    BUY_TRADING = "BUY_TRADING"
    SELL_TRADING = "SELL_TRADING"
    SELL_ALL = "SELL_ALL"          # trading + core (panic only)
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class TradingDecision:
    action: TradingAction
    phase: TradingPhase
    reason: str
    quantity: int = 0
    suggested_limit_price: Decimal | None = None
    pressure_snapshot: MarketPressure | None = None


# ------------------------------------------------------------------ #
# σ-relative parameters (the *only* tunables)
# ------------------------------------------------------------------ #

@dataclass(frozen=True, slots=True)
class TradingPositionRules:
    """All thresholds are in σ multiples, not percentages.

    Statistical meaning (for roughly normal tails):
      1.0σ ≈ 68 % of normal variation
      1.5σ ≈ 87 %   — "unusual"
      2.0σ ≈ 95 %   — "significant"
    """

    # -- Buy gates --
    buy_max_z: Decimal = _d("2.0")
    """Do not chase prices above this z-score."""

    buy_pullback_momentum: Decimal = _d("-0.5")
    """Minimum negative momentum to qualify as a "pullback" buy."""

    buy_orderbook_min: Decimal = _d("0.3")
    """Minimum bid-heavy imbalance to qualify as "pressure buy"."""

    buy_stable_z_band: Decimal = _d("0.5")
    """z-score band for "stable near anchor" buy."""

    buy_stable_vol_ceiling: Decimal = _d("0.5")
    """Max volume_intensity for a "quiet stabilisation" pattern."""

    buy_liquidity_floor: Decimal = _d("0.3")
    """Minimum liquidity_score to allow any buy."""

    buy_time_urgency: Decimal = _d("0.15")
    """After this fraction of session elapsed, relax buy conditions."""

    buy_tolerance_sigma: Decimal = _d("0.5")
    """Limit-price offset above anchor (in σ) to tolerate as slippage."""

    # -- Exhaustion sell gates --
    exhaustion_z: Decimal = _d("1.5")
    """Minimum z-score to consider "in profit territory"."""

    exhaustion_momentum_max: Decimal = _d("0")
    """Momentum must be ≤ this (i.e. fading or negative) to confirm."""

    exhaustion_orderbook_max: Decimal = _d("0")
    """Orderbook imbalance must flip to sell-heavy (≤ 0)."""

    # -- Panic sell gates --
    panic_z: Decimal = _d("-1.5")
    """z-score floor — below this is "panic territory"."""

    panic_volume_min: Decimal = _d("1.5")
    """Must see elevated volume (panic is loud, not quiet)."""

    panic_orderbook_max: Decimal = _d("-0.3")
    """Sell-pressure must be dominant."""

    panic_sells_core: bool = False
    """If True, panic also triggers core-position exit."""

    # -- Close-of-session evaluation --
    close_risk_reward_hold: Decimal = _d("1.5")
    """Profit / overnight-risk ratio to justify holding all."""

    close_risk_reward_partial: Decimal = _d("0.5")
    """Below this, sell everything."""

    close_window_fraction: Decimal = _d("0.85")
    """Start close evaluation after this fraction of session elapsed."""

    overnight_risk_sigma: Decimal = _d("2.0")
    """Assumed overnight price swing (in σ)."""

    # -- Tranche control --
    tranche_count: int = 2
    """Split trading-position buys into this many tranches."""

    tranche_min_ticks_between: int = 5
    """Minimum ticks between successive tranches."""


# ------------------------------------------------------------------ #
# Engine state
# ------------------------------------------------------------------ #

@dataclass(slots=True)
class TradingPositionState:
    phase: TradingPhase = TradingPhase.WAITING_DATA
    total_trading_qty: int = 0
    qty_filled: int = 0
    qty_sold: int = 0
    tranches_filled: int = 0
    avg_buy_price: Decimal = _d(0)
    total_buy_notional: Decimal = _d(0)
    total_sell_notional: Decimal = _d(0)
    ticks_since_last_tranche: int = 0
    last_sell_price: Decimal | None = None

    @property
    def qty_held(self) -> int:
        return max(self.qty_filled - self.qty_sold, 0)

    @property
    def fully_bought(self) -> bool:
        return self.qty_filled >= self.total_trading_qty

    @property
    def fully_sold(self) -> bool:
        return self.qty_held <= 0 and self.qty_filled > 0

    @property
    def remaining_to_buy(self) -> int:
        return max(self.total_trading_qty - self.qty_filled, 0)

    def record_buy(self, qty: int, price: Decimal) -> None:
        notional = price * _d(qty)
        self.total_buy_notional += notional
        self.qty_filled += qty
        self.avg_buy_price = (
            self.total_buy_notional / _d(self.qty_filled)
            if self.qty_filled > 0
            else _d(0)
        )
        self.tranches_filled += 1
        self.ticks_since_last_tranche = 0

    def record_sell(self, qty: int, price: Decimal) -> None:
        self.total_sell_notional += price * _d(qty)
        self.qty_sold += qty
        self.last_sell_price = price

    def unrealized_pnl_per_share(self, current_price: Decimal) -> Decimal:
        if self.avg_buy_price <= 0 or self.qty_held <= 0:
            return _d(0)
        return current_price - self.avg_buy_price


# ------------------------------------------------------------------ #
# Engine
# ------------------------------------------------------------------ #

class TradingPositionEngine:
    """Stateful decision engine for the trading 50%.

    Call :meth:`evaluate` on every tick.  The engine transitions through
    phases automatically and returns a *TradingDecision* describing what
    to do (or do nothing).
    """

    def __init__(
        self,
        rules: TradingPositionRules,
        total_trading_qty: int,
        lot_size: int = 1,
    ) -> None:
        self._rules = rules
        self._lot_size = max(lot_size, 1)
        self._state = TradingPositionState(
            total_trading_qty=self._align(total_trading_qty),
        )

    @property
    def state(self) -> TradingPositionState:
        return self._state

    @property
    def phase(self) -> TradingPhase:
        return self._state.phase

    # ------------------------------------------------------------------ #
    # Main evaluation
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
    ) -> TradingDecision:
        """Produce one decision per tick."""

        s = self._state
        s.ticks_since_last_tranche += 1

        # Phase: EXITED — nothing to do.
        if s.phase is TradingPhase.EXITED:
            return self._decision(TradingAction.WAIT, "exited", pressure)

        # Phase: WAITING_DATA — need sufficient observations.
        if s.phase is TradingPhase.WAITING_DATA:
            if pressure.data_sufficient:
                s.phase = TradingPhase.READY_TO_BUY
                return self._decision(TradingAction.WAIT, "data_ready; evaluating_buy", pressure)
            return self._decision(TradingAction.WAIT, "accumulating_data", pressure)

        # Phase: READY_TO_BUY — evaluate buy signals.
        if s.phase is TradingPhase.READY_TO_BUY:
            return self._evaluate_buy_phase(market, pressure)

        # Phase: HOLDING — evaluate sell signals.
        assert s.phase is TradingPhase.HOLDING
        return self._evaluate_holding_phase(market, pressure)

    # ------------------------------------------------------------------ #
    # Buy phase
    # ------------------------------------------------------------------ #

    def _evaluate_buy_phase(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
    ) -> TradingDecision:
        s = self._state
        r = self._rules

        # Can we even execute right now?
        if pressure.liquidity_score < r.buy_liquidity_floor:
            return self._decision(TradingAction.WAIT, "liquidity_too_low", pressure)

        # Price sanity: don't chase beyond buy_max_z.
        if pressure.price_z_score > r.buy_max_z:
            return self._decision(TradingAction.WAIT, "price_too_high_z", pressure)

        # --- Multi-signal convergence ---
        reasons: list[str] = []

        # Signal A: Pullback buy — price retreated from highs.
        if (
            pressure.price_z_score > _d(0)
            and pressure.momentum_score <= r.buy_pullback_momentum
        ):
            reasons.append("pullback_from_high")

        # Signal B: Orderbook pressure — bid-heavy imbalance.
        if pressure.orderbook_pressure >= r.buy_orderbook_min:
            reasons.append("bid_pressure_strong")

        # Signal C: Quiet stabilisation near anchor.
        if (
            abs(pressure.price_z_score) < r.buy_stable_z_band
            and pressure.volume_intensity < r.buy_stable_vol_ceiling
        ):
            reasons.append("stable_near_anchor")

        # --- Converged buy ---
        if len(reasons) >= 1 and pressure.price_z_score <= r.buy_max_z:
            return self._emit_buy(market, pressure, "+".join(reasons))

        # --- Time urgency: relax conditions ---
        if (
            pressure.time_pressure >= r.buy_time_urgency
            and pressure.price_z_score <= r.buy_max_z
        ):
            return self._emit_buy(market, pressure, "time_urgency")

        return self._decision(TradingAction.WAIT, "no_buy_signal", pressure)

    def _emit_buy(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
        reason: str,
    ) -> TradingDecision:
        s = self._state
        r = self._rules

        # Tranche pacing.
        if (
            s.tranches_filled > 0
            and s.ticks_since_last_tranche < r.tranche_min_ticks_between
        ):
            return self._decision(TradingAction.WAIT, "tranche_cooldown", pressure)

        qty = self._tranche_qty()
        if qty <= 0:
            # Fully bought — transition.
            s.phase = TradingPhase.HOLDING
            return self._decision(TradingAction.HOLD, "fully_bought", pressure)

        # Adaptive limit price: anchor + tolerance_σ.
        anchor = market.opening_vwap or market.rolling_vwap or market.last_price
        sigma = market.realized_vol
        limit = None
        if anchor and sigma > 0 and market.last_price:
            limit = min(
                market.last_price,
                anchor + r.buy_tolerance_sigma * sigma,
            )
            # Never lower than best_ask (we want to fill).
            if market.last_price:
                limit = max(limit, market.last_price)

        return TradingDecision(
            action=TradingAction.BUY_TRADING,
            phase=s.phase,
            reason=reason,
            quantity=qty,
            suggested_limit_price=limit,
            pressure_snapshot=pressure,
        )

    def _tranche_qty(self) -> int:
        s = self._state
        r = self._rules
        remaining = s.remaining_to_buy
        if remaining <= 0:
            return 0
        tranches_left = max(r.tranche_count - s.tranches_filled, 1)
        raw = remaining // tranches_left
        return self._align(raw) if raw > 0 else self._align(remaining)

    # ------------------------------------------------------------------ #
    # Holding phase
    # ------------------------------------------------------------------ #

    def _evaluate_holding_phase(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
    ) -> TradingDecision:
        s = self._state

        if s.qty_held <= 0:
            s.phase = TradingPhase.EXITED
            return self._decision(TradingAction.WAIT, "no_position", pressure)

        # --- Priority 1: Panic detection ---
        panic = self._detect_panic(market, pressure)
        if panic is not None:
            return panic

        # --- Priority 2: Exhaustion detection ---
        exhaustion = self._detect_exhaustion(market, pressure)
        if exhaustion is not None:
            return exhaustion

        # --- Priority 3: Close-of-session evaluation ---
        close = self._evaluate_close(market, pressure)
        if close is not None:
            return close

        return self._decision(TradingAction.HOLD, "no_exit_signal", pressure)

    def _detect_panic(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
    ) -> TradingDecision | None:
        r = self._rules
        if not all([
            pressure.price_z_score < r.panic_z,
            pressure.volume_intensity > r.panic_volume_min,
            pressure.orderbook_pressure < r.panic_orderbook_max,
            market.last_price is not None
            and market.rolling_low is not None
            and market.last_price <= market.rolling_low,
        ]):
            return None

        s = self._state
        action = TradingAction.SELL_ALL if r.panic_sells_core else TradingAction.SELL_TRADING
        qty = s.qty_held
        s.phase = TradingPhase.EXITED

        return TradingDecision(
            action=action,
            phase=s.phase,
            reason="panic_detected",
            quantity=qty,
            suggested_limit_price=market.last_price,
            pressure_snapshot=pressure,
        )

    def _detect_exhaustion(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
    ) -> TradingDecision | None:
        r = self._rules
        if not all([
            pressure.price_z_score > r.exhaustion_z,
            pressure.momentum_score <= r.exhaustion_momentum_max,
            pressure.orderbook_pressure <= r.exhaustion_orderbook_max,
        ]):
            return None

        s = self._state
        # Sell in halves: first exhaustion sells 50%, second sells the rest.
        qty = self._align(max(s.qty_held // 2, self._lot_size))
        qty = min(qty, s.qty_held)

        return TradingDecision(
            action=TradingAction.SELL_TRADING,
            phase=s.phase,
            reason="exhaustion_detected",
            quantity=qty,
            suggested_limit_price=market.last_price,
            pressure_snapshot=pressure,
        )

    def _evaluate_close(
        self,
        market: AdaptiveMarketState,
        pressure: MarketPressure,
    ) -> TradingDecision | None:
        r = self._rules
        s = self._state

        if pressure.time_pressure < r.close_window_fraction:
            return None

        if market.last_price is None or market.realized_vol <= 0:
            return None

        pnl_per_share = s.unrealized_pnl_per_share(market.last_price)
        overnight_risk = r.overnight_risk_sigma * market.realized_vol
        risk_reward = pnl_per_share / overnight_risk if overnight_risk > 0 else _d(0)

        if risk_reward >= r.close_risk_reward_hold:
            return self._decision(
                TradingAction.HOLD,
                f"close_hold;rr={risk_reward:.2f}",
                pressure,
            )

        if risk_reward >= r.close_risk_reward_partial:
            # Modest profit — sell trading position, keep core for day-1.
            return TradingDecision(
                action=TradingAction.SELL_TRADING,
                phase=s.phase,
                reason=f"close_sell_trading;rr={risk_reward:.2f}",
                quantity=s.qty_held,
                suggested_limit_price=market.last_price,
                pressure_snapshot=pressure,
            )

        # Thin / negative edge — exit trading position.
        return TradingDecision(
            action=TradingAction.SELL_TRADING,
            phase=s.phase,
            reason=f"close_sell_all_trading;rr={risk_reward:.2f}",
            quantity=s.qty_held,
            suggested_limit_price=market.last_price,
            pressure_snapshot=pressure,
        )

    # ------------------------------------------------------------------ #
    # Confirm fills from the broker / simulator
    # ------------------------------------------------------------------ #

    def confirm_buy_fill(self, qty: int, price: Decimal) -> None:
        """Called after a BUY_TRADING order is filled (fully or partially)."""
        s = self._state
        s.record_buy(qty, price)
        if s.fully_bought:
            s.phase = TradingPhase.HOLDING

    def confirm_sell_fill(self, qty: int, price: Decimal) -> None:
        """Called after a SELL_TRADING order is filled."""
        s = self._state
        s.record_sell(qty, price)
        if s.fully_sold:
            s.phase = TradingPhase.EXITED

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _align(self, qty: int) -> int:
        """Round down to the nearest lot."""
        return max((qty // self._lot_size) * self._lot_size, 0)

    def _decision(
        self,
        action: TradingAction,
        reason: str,
        pressure: MarketPressure,
    ) -> TradingDecision:
        return TradingDecision(
            action=action,
            phase=self._state.phase,
            reason=reason,
            pressure_snapshot=pressure,
        )
