"""Intraday adaptive market state for same-day dry-run cost reduction."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt


@dataclass(frozen=True, slots=True)
class AdaptiveMarketState:
    opening_vwap: Decimal | None
    rolling_vwap: Decimal | None
    realized_vol: Decimal
    rolling_high: Decimal | None
    rolling_low: Decimal | None
    cumulative_turnover: Decimal
    volume_delta: Decimal
    turnover_delta: Decimal
    cumulative_field_reset_detected: bool
    tick_count: int
    orderbook_imbalance: Decimal
    spread_bps: Decimal
    last_price: Decimal | None


class IntradayAdaptiveTracker:
    def __init__(self, *, window_size: int = 30) -> None:
        self._window_size = max(window_size, 5)
        self._prices: deque[Decimal] = deque(maxlen=self._window_size)
        self._rolling_vwap_inputs: deque[tuple[Decimal, Decimal]] = deque(
            maxlen=self._window_size
        )
        self._cum_turnover = Decimal("0")
        self._cum_notional = Decimal("0")
        self._cum_volume = Decimal("0")
        self._tick_count = 0
        self.previous_volume: Decimal | None = None
        self.previous_turnover: Decimal | None = None

    @staticmethod
    def _to_decimal(value) -> Decimal | None:
        if value in {None, "", "N/A"}:
            return None
        return Decimal(str(value))

    def update_from_signal(self, signal) -> AdaptiveMarketState:
        raw_quote = signal.raw_quote or {}
        best_bid = self._to_decimal(signal.best_bid)
        best_ask = self._to_decimal(signal.best_ask)
        bid_qty = Decimal(str(signal.bid_quantity or 0))
        ask_qty = Decimal(str(signal.ask_quantity or 0))

        last_price = self._to_decimal(raw_quote.get("last_price"))
        if last_price is None or last_price <= 0:
            candidates = [best_ask, best_bid]
            last_price = next((candidate for candidate in candidates if candidate and candidate > 0), None)

        turnover = self._to_decimal(raw_quote.get("turnover"))
        if turnover is None:
            turnover = Decimal("0")

        volume = self._to_decimal(raw_quote.get("volume"))
        if volume is None:
            volume = Decimal("0")

        volume_delta = Decimal("0")
        cumulative_field_reset_detected = False
        if self.previous_volume is not None:
            raw_volume_delta = volume - self.previous_volume
            if raw_volume_delta >= 0:
                volume_delta = raw_volume_delta
            else:
                cumulative_field_reset_detected = True
        self.previous_volume = volume

        turnover_delta = Decimal("0")
        if self.previous_turnover is not None:
            raw_turnover_delta = turnover - self.previous_turnover
            if raw_turnover_delta >= 0:
                turnover_delta = raw_turnover_delta
            else:
                cumulative_field_reset_detected = True
        self.previous_turnover = turnover

        if last_price is not None and last_price > 0:
            self._prices.append(last_price)
            self._tick_count += 1
            self._cum_turnover += turnover_delta
            if volume_delta > 0:
                self._cum_notional += turnover_delta
                self._cum_volume += volume_delta
                self._rolling_vwap_inputs.append((last_price, volume_delta))

        opening_vwap = None
        if self._cum_volume > 0:
            opening_vwap = self._cum_notional / self._cum_volume

        rolling_vwap = None
        if self._rolling_vwap_inputs:
            rolling_volume = sum(item[1] for item in self._rolling_vwap_inputs)
            if rolling_volume > 0:
                rolling_notional = sum(
                    price * volume for price, volume in self._rolling_vwap_inputs
                )
                rolling_vwap = rolling_notional / rolling_volume

        realized_vol = Decimal("0")
        if len(self._prices) >= 2:
            mean = float(sum(self._prices) / Decimal(len(self._prices)))
            variance = sum((float(price) - mean) ** 2 for price in self._prices) / max(len(self._prices) - 1, 1)
            realized_vol = Decimal(str(sqrt(max(variance, 0.0))))

        rolling_high = max(self._prices) if self._prices else None
        rolling_low = min(self._prices) if self._prices else None

        spread_bps = Decimal("0")
        if best_bid and best_ask and best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / Decimal("2")
            if mid > 0:
                spread_bps = ((best_ask - best_bid) / mid) * Decimal("10000")

        imbalance = Decimal("0")
        total_qty = bid_qty + ask_qty
        if total_qty > 0:
            imbalance = (bid_qty - ask_qty) / total_qty

        return AdaptiveMarketState(
            opening_vwap=opening_vwap,
            rolling_vwap=rolling_vwap,
            realized_vol=realized_vol,
            rolling_high=rolling_high,
            rolling_low=rolling_low,
            cumulative_turnover=self._cum_turnover,
            volume_delta=volume_delta,
            turnover_delta=turnover_delta,
            cumulative_field_reset_detected=cumulative_field_reset_detected,
            tick_count=self._tick_count,
            orderbook_imbalance=imbalance,
            spread_bps=spread_bps,
            last_price=last_price,
        )
