"""Read-only Futu OpenD futures helpers.

This module intentionally does not submit futures orders.  It only probes
OpenD futures support and maps `get_future_info` rows into auditable local
objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import socket
from typing import Any, Iterable

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.contracts import AssetClass, ContractSpec, ContractSpecError
from futu_opend_execution.execution.broker import BrokerConfigurationError
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.execution.market_data import MarketDataResponseError
from futu_opend_execution.risk import validate_runtime_config


@dataclass(frozen=True, slots=True)
class FutuFutureInfo:
    code: str
    name: str
    owner: str | None
    exchange: str
    contract_type: str
    contract_size: Decimal
    contract_size_unit: str | None
    price_currency: str | None
    price_unit: str | None
    min_change: Decimal
    min_change_unit: str | None
    trade_time: str | None
    time_zone: str | None
    last_trade_time: str | None
    exchange_format_url: str | None
    origin_code: str | None
    raw: dict[str, Any]

    def to_jsonable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contract_size"] = _str_decimal(self.contract_size)
        payload["min_change"] = _str_decimal(self.min_change)
        return payload

    def to_contract_spec(self, *, margin_rate: Decimal | str | int | float = Decimal("0"), commission_per_contract: Decimal | str | int | float = Decimal("0")) -> ContractSpec:
        if not _is_index_future_type(self.contract_type):
            raise ContractSpecError(f"future type is not an index future: {self.contract_type}")
        return ContractSpec(
            symbol=self.code,
            exchange=self.exchange or "UNKNOWN",
            asset_class=AssetClass.INDEX_FUTURE,
            contract_multiplier=self.contract_size,
            tick_size=self.min_change,
            currency=self.price_currency or "HKD",
            min_order_size=1,
            margin_rate=margin_rate,
            commission_per_contract=commission_per_contract,
            session_timezone=self.time_zone or "Asia/Hong_Kong",
            trading_sessions=tuple(_split_trade_time(self.trade_time)),
            expiry_date=self.last_trade_time or None,
            rollover_group=self.owner or self.origin_code or None,
            notes="Derived from Futu OpenD get_future_info; verify margin and fees separately.",
        )


class FutuOpenDFuturesClient:
    """Read-only futures quote/metadata adapter backed by `futu-api`."""

    def __init__(self, config: RuntimeConfig | None = None, *, socket_timeout_seconds: float = 1.0) -> None:
        self._config = config or RuntimeConfig.from_env()
        validate_runtime_config(self._config)
        _ensure_opend_socket(self._config, timeout_seconds=socket_timeout_seconds)
        self._futu = load_futu_module(self._config)
        self._quote_context = self._futu.OpenQuoteContext(
            host=self._config.futu_host,
            port=self._config.futu_port,
        )

    def __enter__(self) -> "FutuOpenDFuturesClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._quote_context.close()

    def get_future_info(self, symbols: Iterable[str]) -> list[FutuFutureInfo]:
        codes = [_normalize_symbol(symbol) for symbol in symbols]
        if not codes:
            raise MarketDataResponseError("at least one futures symbol is required")
        if not hasattr(self._quote_context, "get_future_info"):
            raise MarketDataResponseError("futu-api does not expose get_future_info")
        ret, data = self._quote_context.get_future_info(codes)
        if ret != self._futu.RET_OK:
            raise MarketDataResponseError(f"get_future_info failed: {data}")
        return [_row_to_future_info(row) for row in _rows(data)]

    def probe_future_trade_context(self) -> dict[str, Any]:
        if not hasattr(self._futu, "OpenFutureTradeContext"):
            raise BrokerConfigurationError("futu-api does not expose OpenFutureTradeContext")
        ctx = self._futu.OpenFutureTradeContext(
            host=self._config.futu_host,
            port=self._config.futu_port,
            security_firm=_resolve_security_firm(self._futu, self._config.futu_security_firm),
        )
        try:
            return {
                "ok": True,
                "context": "OpenFutureTradeContext",
                "host": self._config.futu_host,
                "port": self._config.futu_port,
                "read_only": True,
            }
        finally:
            ctx.close()


def _row_to_future_info(row: dict[str, Any]) -> FutuFutureInfo:
    return FutuFutureInfo(
        code=str(row.get("code") or "").strip().upper(),
        name=str(row.get("name") or ""),
        owner=_optional_string(row.get("owner")),
        exchange=str(row.get("exchange") or ""),
        contract_type=str(row.get("type") or row.get("contract_type") or ""),
        contract_size=_decimal(row.get("size") or row.get("contract_size") or 0),
        contract_size_unit=_optional_string(row.get("size_unit") or row.get("contract_size_unit")),
        price_currency=_optional_string(row.get("price_currency") or row.get("quote_currency")),
        price_unit=_optional_string(row.get("price_unit") or row.get("quote_unit")),
        min_change=_decimal(row.get("min_change") or row.get("min_var") or 0),
        min_change_unit=_optional_string(row.get("min_change_unit") or row.get("min_var_unit")),
        trade_time=_optional_string(row.get("trade_time")),
        time_zone=_optional_string(row.get("time_zone")),
        last_trade_time=_optional_string(row.get("last_trade_time")),
        exchange_format_url=_optional_string(row.get("exchange_format_url")),
        origin_code=_optional_string(row.get("origin_code")),
        raw=row,
    )


def _rows(data) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        return [dict(item) for item in data.to_dict("records")]
    if isinstance(data, list):
        return [dict(item) for item in data]
    raise MarketDataResponseError(f"Unsupported future info payload type: {type(data).__name__}")


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        return normalized
    return f"HK.{normalized}"


def _resolve_security_firm(futu, name: str):
    normalized = name.strip().upper()
    try:
        return getattr(futu.SecurityFirm, normalized)
    except AttributeError as exc:
        raise BrokerConfigurationError(f"Unsupported FUTU_SECURITY_FIRM value: {name!r}") from exc


def _ensure_opend_socket(config: RuntimeConfig, *, timeout_seconds: float) -> None:
    try:
        with socket.create_connection((config.futu_host, config.futu_port), timeout=timeout_seconds):
            return
    except OSError as exc:
        raise MarketDataResponseError(f"OpenD is not reachable at {config.futu_host}:{config.futu_port}") from exc


def _is_index_future_type(value: str) -> bool:
    normalized = value.strip().upper()
    return "股指" in value or "INDEX" in normalized


def _split_trade_time(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().strip("()") for item in value.split(",") if item.strip()]


def _optional_string(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _str_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
