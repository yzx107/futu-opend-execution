"""Futu OpenD trade broker adapter."""

from __future__ import annotations

from decimal import Decimal

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import (
    BrokerConfigurationError,
    BrokerOrderNotFoundError,
    BrokerResponseError,
)
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.models import BrokerOrderSnapshot, TimeInForce, TradeMode
from futu_opend_execution.risk import validate_runtime_config


class FutuOpenDTradeBroker:
    """Broker adapter backed by the optional `futu-api` Python SDK."""

    supports_native_ioc = False

    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self._config = config or RuntimeConfig.from_env()
        validate_runtime_config(self._config)
        self._futu = load_futu_module(self._config)
        self._trade_context = self._futu.OpenSecTradeContext(
            filter_trdmarket=self._futu.TrdMarket.HK,
            host=self._config.futu_host,
            port=self._config.futu_port,
            security_firm=self._resolve_security_firm(self._config.futu_security_firm),
        )
        self._trade_unlocked = False

    def __enter__(self) -> "FutuOpenDTradeBroker":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def place_limit_buy(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: Decimal,
        trade_mode: TradeMode,
        time_in_force: TimeInForce,
        remark: str | None = None,
    ) -> BrokerOrderSnapshot:
        return self._place_limit_order(
            symbol=symbol,
            quantity=quantity,
            limit_price=limit_price,
            side=self._futu.TrdSide.BUY,
            trade_mode=trade_mode,
            time_in_force=time_in_force,
            remark=remark,
        )

    def place_limit_sell(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: Decimal,
        trade_mode: TradeMode,
        time_in_force: TimeInForce,
        remark: str | None = None,
    ) -> BrokerOrderSnapshot:
        return self._place_limit_order(
            symbol=symbol,
            quantity=quantity,
            limit_price=limit_price,
            side=self._futu.TrdSide.SELL,
            trade_mode=trade_mode,
            time_in_force=time_in_force,
            remark=remark,
        )

    def _place_limit_order(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: Decimal,
        side,
        trade_mode: TradeMode,
        time_in_force: TimeInForce,
        remark: str | None,
    ) -> BrokerOrderSnapshot:
        if trade_mode is TradeMode.REAL:
            self._ensure_trade_unlocked()

        ret, data = self._trade_context.place_order(
            price=float(limit_price),
            qty=float(quantity),
            code=self._normalize_symbol(symbol),
            trd_side=side,
            order_type=self._futu.OrderType.NORMAL,
            trd_env=self._resolve_trade_env(trade_mode),
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
            remark=remark,
            time_in_force=self._resolve_time_in_force(time_in_force),
            session=self._futu.Session.NONE,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"place_order failed: {data}")

        row = self._pick_single_row(data)
        return self._row_to_snapshot(row, fallback_symbol=symbol)

    def get_order(
        self,
        *,
        order_id: str,
        symbol: str,
        trade_mode: TradeMode,
    ) -> BrokerOrderSnapshot:
        broker_symbol = self._normalize_symbol(symbol)
        trd_env = self._resolve_trade_env(trade_mode)

        ret, data = self._trade_context.order_list_query(
            order_id=order_id,
            code=broker_symbol,
            trd_env=trd_env,
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
            refresh_cache=True,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"order_list_query failed: {data}")

        row = self._find_order_row(self._rows_from_table(data), order_id=order_id)
        if row is None:
            ret, data = self._trade_context.history_order_list_query(
                code=broker_symbol,
                trd_env=trd_env,
                acc_id=self._config.futu_acc_id,
                acc_index=self._config.futu_acc_index,
            )
            if ret != self._futu.RET_OK:
                raise BrokerResponseError(f"history_order_list_query failed: {data}")
            row = self._find_order_row(self._rows_from_table(data), order_id=order_id)

        if row is None:
            raise BrokerOrderNotFoundError(f"Unable to locate broker order {order_id}.")

        return self._row_to_snapshot(row, fallback_symbol=symbol)

    def cancel_order(
        self,
        *,
        order_id: str,
        symbol: str,
        trade_mode: TradeMode,
    ) -> None:
        del symbol

        if trade_mode is TradeMode.REAL:
            self._ensure_trade_unlocked()

        ret, data = self._trade_context.modify_order(
            modify_order_op=self._futu.ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=self._resolve_trade_env(trade_mode),
            acc_id=self._config.futu_acc_id,
            acc_index=self._config.futu_acc_index,
        )
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"modify_order cancel failed: {data}")

    def close(self) -> None:
        self._trade_context.close()

    def _resolve_security_firm(self, name: str):
        normalized = name.strip().upper()
        try:
            return getattr(self._futu.SecurityFirm, normalized)
        except AttributeError as exc:
            raise BrokerConfigurationError(
                f"Unsupported FUTU_SECURITY_FIRM value: {name!r}"
            ) from exc

    def _resolve_trade_env(self, trade_mode: TradeMode):
        if trade_mode is TradeMode.SIMULATED:
            return self._futu.TrdEnv.SIMULATE
        return self._futu.TrdEnv.REAL

    def _resolve_time_in_force(self, time_in_force: TimeInForce):
        if time_in_force is TimeInForce.DAY:
            return self._futu.TimeInForce.DAY
        if time_in_force is TimeInForce.GTC:
            return self._futu.TimeInForce.GTC
        raise BrokerConfigurationError(
            "Futu OpenD does not expose a native IOC time-in-force for this flow. "
            "Use the execution service to emulate IOC via cancel-on-timeout."
        )

    def _ensure_trade_unlocked(self) -> None:
        if self._trade_unlocked:
            return
        if self._config.futu_trade_password is None:
            raise BrokerConfigurationError(
                "FUTU_TRADE_PASSWORD is required for real trading unlock."
            )
        ret, data = self._trade_context.unlock_trade(self._config.futu_trade_password)
        if ret != self._futu.RET_OK:
            raise BrokerResponseError(f"unlock_trade failed: {data}")
        self._trade_unlocked = True

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if "." in normalized:
            return normalized
        return f"HK.{normalized}"

    @staticmethod
    def _rows_from_table(table) -> list[dict[str, object]]:
        if hasattr(table, "to_dict"):
            return [dict(row) for row in table.to_dict("records")]
        if isinstance(table, list):
            return [dict(row) for row in table]
        raise BrokerResponseError(
            f"Unsupported broker payload type: {type(table).__name__}"
        )

    def _pick_single_row(self, table) -> dict[str, object]:
        rows = self._rows_from_table(table)
        if not rows:
            raise BrokerResponseError("Broker returned an empty order payload.")
        return rows[0]

    @staticmethod
    def _find_order_row(
        rows: list[dict[str, object]],
        *,
        order_id: str,
    ) -> dict[str, object] | None:
        for row in rows:
            if str(row.get("order_id")) == str(order_id):
                return row
        return None

    @staticmethod
    def _row_to_snapshot(
        row: dict[str, object],
        *,
        fallback_symbol: str,
    ) -> BrokerOrderSnapshot:
        code = str(row.get("code") or fallback_symbol).strip().upper()
        if "." in code:
            _, code = code.split(".", 1)

        return BrokerOrderSnapshot(
            order_id=str(row.get("order_id")),
            symbol=code,
            status=str(row.get("order_status") or "UNKNOWN"),
            quantity=int(float(row.get("qty", 0) or 0)),
            price=FutuOpenDTradeBroker._to_decimal(row.get("price")),
            dealt_quantity=int(float(row.get("dealt_qty", 0) or 0)),
            dealt_avg_price=FutuOpenDTradeBroker._to_optional_decimal(
                row.get("dealt_avg_price")
            ),
            updated_time=FutuOpenDTradeBroker._to_optional_string(
                row.get("updated_time")
            ),
            message=FutuOpenDTradeBroker._to_optional_string(
                row.get("last_err_msg")
            )
            or "",
            raw_payload=row,
        )

    @staticmethod
    def _to_decimal(value) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _to_optional_decimal(value) -> Decimal | None:
        if value in {None, "", "N/A"}:
            return None
        return Decimal(str(value))

    @staticmethod
    def _to_optional_string(value) -> str | None:
        if value in {None, "", "N/A"}:
            return None
        return str(value)
