"""Multi-dimensional market pressure calculator for grey-market trading.

All thresholds are expressed in units of realized volatility (σ), not fixed
percentages.  The calculator normalises raw AdaptiveMarketState fields into
a standardised MarketPressure snapshot so that downstream decision engines
can operate on a consistent, stock-agnostic scale.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState

# Grey-market session: 16:15 – 18:30 = 2h15m = 8100 seconds.
DEFAULT_SESSION_SECONDS = Decimal("8100")


def _d(value: int | float | str) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class MarketPressure:
    """Normalised, multi-signal snapshot for one tick.

    Every field is dimensionless and comparable across stocks with different
    price levels and volatility profiles.
    """

    # (price − anchor) / σ.  Positive = above fair value.
    price_z_score: Decimal

    # Net directional momentum in σ units.
    # Positive = advancing from lows, negative = retreating from highs.
    momentum_score: Decimal

    # Order-book imbalance, already in [−1, +1].
    # Positive = bid-heavy (buying pressure).
    orderbook_pressure: Decimal

    # Current volume delta / average volume delta.
    # >1 = elevated activity, <1 = quiet.
    volume_intensity: Decimal

    # Liquidity quality indicator [0, 1].
    # Higher = tighter spread, easier to execute.
    liquidity_score: Decimal

    # Session time elapsed [0, 1].
    # 0 = just opened, 1 = about to close.
    time_pressure: Decimal

    # True when the tracker has accumulated enough observations for the
    # above fields to be statistically meaningful.
    data_sufficient: bool


class MarketPressureCalculator:
    """Produces a *MarketPressure* from every *AdaptiveMarketState* update.

    Internally maintains small rolling buffers for volume-delta averaging and
    spread-history tracking.  Everything else comes from the upstream adaptive
    tracker.
    """

    def __init__(
        self,
        *,
        window_size: int = 30,
        min_ticks_for_data: int = 10,
        session_seconds: Decimal = DEFAULT_SESSION_SECONDS,
        spread_ceiling_bps: Decimal = _d("200"),
    ) -> None:
        self._window_size = max(window_size, 5)
        self._min_ticks = min_ticks_for_data
        self._session_seconds = session_seconds
        self._spread_ceiling_bps = spread_ceiling_bps
        self._volume_deltas: deque[Decimal] = deque(maxlen=self._window_size)
        self._spread_history: deque[Decimal] = deque(maxlen=self._window_size)

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #

    def compute(
        self,
        market: AdaptiveMarketState,
        *,
        elapsed_seconds: float,
    ) -> MarketPressure:
        """Derive a normalised *MarketPressure* from *market* state."""

        sufficient = self._has_sufficient_data(market)
        anchor = market.opening_vwap or market.rolling_vwap
        sigma = market.realized_vol

        price_z = self._price_z_score(market.last_price, anchor, sigma)
        momentum = self._momentum_score(market, sigma)
        orderbook = market.orderbook_imbalance
        vol_intensity = self._volume_intensity(market.volume_delta)
        liquidity = self._liquidity_score(market.spread_bps)
        time_p = self._time_pressure(elapsed_seconds)

        return MarketPressure(
            price_z_score=price_z,
            momentum_score=momentum,
            orderbook_pressure=orderbook,
            volume_intensity=vol_intensity,
            liquidity_score=liquidity,
            time_pressure=time_p,
            data_sufficient=sufficient,
        )

    # --------------------------------------------------------------------- #
    # Internals
    # --------------------------------------------------------------------- #

    def _has_sufficient_data(self, market: AdaptiveMarketState) -> bool:
        return (
            market.realized_vol > 0
            and market.opening_vwap is not None
            and market.tick_count >= self._min_ticks
        )

    @staticmethod
    def _price_z_score(
        price: Decimal | None,
        anchor: Decimal | None,
        sigma: Decimal,
    ) -> Decimal:
        if price is None or anchor is None or sigma <= 0:
            return _d(0)
        return (price - anchor) / sigma

    @staticmethod
    def _momentum_score(market: AdaptiveMarketState, sigma: Decimal) -> Decimal:
        if (
            market.last_price is None
            or market.rolling_high is None
            or market.rolling_low is None
            or sigma <= 0
        ):
            return _d(0)
        retreat = (market.rolling_high - market.last_price) / sigma
        advance = (market.last_price - market.rolling_low) / sigma
        return advance - retreat

    def _volume_intensity(self, volume_delta: Decimal) -> Decimal:
        self._volume_deltas.append(volume_delta)
        total = sum(self._volume_deltas)
        count = len(self._volume_deltas)
        if count == 0 or total <= 0:
            return _d(0)
        avg = total / _d(count)
        if avg <= 0:
            return _d(0)
        return volume_delta / avg

    def _liquidity_score(self, spread_bps: Decimal) -> Decimal:
        self._spread_history.append(spread_bps)
        # 0-bps spread → 1.0, ≥ceiling → 0.0
        score = max(_d(1) - spread_bps / self._spread_ceiling_bps, _d(0))
        return min(score, _d(1))

    def _time_pressure(self, elapsed_seconds: float) -> Decimal:
        if self._session_seconds <= 0:
            return _d(0)
        ratio = _d(elapsed_seconds) / self._session_seconds
        return min(max(ratio, _d(0)), _d(1))
