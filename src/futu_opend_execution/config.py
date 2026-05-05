"""Runtime configuration for the execution layer."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from os import environ
from typing import Mapping, MutableMapping


LOCAL_OPEND_HOSTS = {"localhost"}
LOCAL_OPEND_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"Unsupported boolean value: {value!r}")


def _parse_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _parse_float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    return float(value)


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_csv_tuple(value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    return items or default


def is_local_opend_host(host: str) -> bool:
    """Return whether an OpenD host is guaranteed to stay on this machine."""

    normalized = host.strip().strip("[]").lower()
    if normalized in LOCAL_OPEND_HOSTS:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def harden_local_opend_environment(
    env: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Keep OpenD SDK traffic away from process-level proxy settings.

    Futu OpenD should be reached through loopback TCP. Clearing proxy
    environment variables does not control system-level TUN routing, but it
    avoids accidental SDK/helper HTTP proxy usage in this process.
    """

    target = environ if env is None else env
    removed: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        value = target.pop(key, None)
        if value is not None:
            removed[key] = value

    for key in ("NO_PROXY", "no_proxy"):
        existing = [
            item.strip()
            for item in str(target.get(key, "")).split(",")
            if item.strip()
        ]
        existing_lower = {item.lower() for item in existing}
        for host in LOCAL_OPEND_NO_PROXY_HOSTS:
            if host.lower() not in existing_lower:
                existing.append(host)
                existing_lower.add(host.lower())
        target[key] = ",".join(existing)
    return removed


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Environment-derived runtime settings."""

    futu_host: str = "127.0.0.1"
    futu_port: int = 11111
    allow_real_trade: bool = False
    futu_security_firm: str = "FUTUSECURITIES"
    futu_acc_id: int = 0
    futu_sim_acc_id: int = 0
    futu_acc_index: int = 0
    futu_trade_password: str | None = None
    futu_sdk_home_override: str | None = None
    order_poll_interval_seconds: float = 0.2
    cancel_order_grace_seconds: float = 2.0
    default_ioc_timeout_seconds: float = 1.0
    quote_poll_interval_seconds: float = 0.5
    default_wait_for_open_timeout_seconds: float = 300.0
    default_order_book_depth: int = 10
    grey_market_open_states: tuple[str, ...] = (
        "AUCTION",
        "MORNING",
        "AFTERNOON",
        "AFTER_HOURS_BEGIN",
        "HK_CAS",
        "NIGHT_OPEN",
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeConfig":
        source = environ if env is None else env
        return cls(
            futu_host=source.get("FUTU_HOST", "127.0.0.1"),
            futu_port=_parse_int(source.get("FUTU_PORT"), default=11111),
            allow_real_trade=_parse_bool(
                source.get("FUTU_ALLOW_REAL_TRADE"),
                default=False,
            ),
            futu_security_firm=source.get("FUTU_SECURITY_FIRM", "FUTUSECURITIES"),
            futu_acc_id=_parse_int(source.get("FUTU_ACC_ID"), default=0),
            futu_sim_acc_id=_parse_int(source.get("FUTU_SIM_ACC_ID"), default=0),
            futu_acc_index=_parse_int(source.get("FUTU_ACC_INDEX"), default=0),
            futu_trade_password=_empty_to_none(source.get("FUTU_TRADE_PASSWORD")),
            futu_sdk_home_override=_empty_to_none(
                source.get("FUTU_SDK_HOME_OVERRIDE")
            ),
            order_poll_interval_seconds=_parse_float(
                source.get("FUTU_ORDER_POLL_INTERVAL_SECONDS"),
                default=0.2,
            ),
            cancel_order_grace_seconds=_parse_float(
                source.get("FUTU_CANCEL_ORDER_GRACE_SECONDS"),
                default=2.0,
            ),
            default_ioc_timeout_seconds=_parse_float(
                source.get("FUTU_DEFAULT_IOC_TIMEOUT_SECONDS"),
                default=1.0,
            ),
            quote_poll_interval_seconds=_parse_float(
                source.get("FUTU_QUOTE_POLL_INTERVAL_SECONDS"),
                default=0.5,
            ),
            default_wait_for_open_timeout_seconds=_parse_float(
                source.get("FUTU_DEFAULT_WAIT_FOR_OPEN_TIMEOUT_SECONDS"),
                default=300.0,
            ),
            default_order_book_depth=_parse_int(
                source.get("FUTU_DEFAULT_ORDER_BOOK_DEPTH"),
                default=10,
            ),
            grey_market_open_states=_parse_csv_tuple(
                source.get("FUTU_GREY_MARKET_OPEN_STATES"),
                default=(
                    "AUCTION",
                    "MORNING",
                    "AFTERNOON",
                    "AFTER_HOURS_BEGIN",
                    "HK_CAS",
                    "NIGHT_OPEN",
                ),
            ),
        )
