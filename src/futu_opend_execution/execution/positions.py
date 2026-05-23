"""Read-only OpenD position lookup."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from futu_opend_execution.config import RuntimeConfig, harden_local_opend_environment
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.models import TradeMode
from futu_opend_execution.risk import validate_runtime_config


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    quantity: int
    cost_price: Decimal
    market_value: Decimal | None = None
    raw: dict[str, Any] | None = None


class OpenDPositionProvider:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        harden_local_opend_environment()
        self._config = config or RuntimeConfig.from_env()
        validate_runtime_config(self._config)
        self._futu = load_futu_module(self._config)
        self._trade_ctx = self._futu.OpenSecTradeContext(
            filter_trdmarket=self._futu.TrdMarket.HK,
            host=self._config.futu_host,
            port=self._config.futu_port,
            security_firm=getattr(self._futu.SecurityFirm, self._config.futu_security_firm.strip().upper()),
        )

    def __enter__(self) -> "OpenDPositionProvider":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._trade_ctx.close()

    def list_positions(self, *, trade_mode: TradeMode = TradeMode.REAL) -> list[PositionSnapshot]:
        ret, data = self._trade_ctx.position_list_query(
            trd_env=self._futu.TrdEnv.REAL if trade_mode is TradeMode.REAL else self._futu.TrdEnv.SIMULATE,
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
            refresh_cache=True,
        )
        if ret != self._futu.RET_OK:
            raise RuntimeError(f"position_list_query failed: {data}")
        return [_row_to_position(row) for row in _rows(data)]


def _rows(data) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        return [dict(item) for item in data.to_dict("records")]
    if isinstance(data, list):
        return [dict(item) for item in data]
    return []


def _row_to_position(row: dict[str, Any]) -> PositionSnapshot:
    code = str(row.get("code") or row.get("stock_code") or "").strip().upper()
    return PositionSnapshot(
        symbol=code if "." in code else f"HK.{code}",
        quantity=int(float(row.get("qty") or row.get("can_sell_qty") or 0)),
        cost_price=_decimal(row.get("cost_price") or row.get("average_cost") or 0),
        market_value=_optional_decimal(row.get("market_val") or row.get("market_value")),
        raw=row,
    )


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return _decimal(value)
