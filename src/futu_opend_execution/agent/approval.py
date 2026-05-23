"""Manual approval files for guarded real orders."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from futu_opend_execution._compat import UTC
from futu_opend_execution.execution.orders import (
    OrderRole,
    OrderSide,
    OrderSource,
    RealOrderIntent,
)
from futu_opend_execution.risk import ExecutionValidationError


DEFAULT_CONFIRMATION_PHRASE = "确认实盘"
LIMIT_ORDER_TYPES = {"", "LIMIT", "NORMAL"}


@dataclass(frozen=True, slots=True)
class PendingRealOrderApproval:
    approval_id: str
    signal_id: str
    symbol: str
    side: OrderSide
    role: OrderRole
    quantity: int
    limit_price: Decimal
    expected_edge_bps: Decimal
    created_at: datetime
    expires_at: datetime
    approved: bool
    approved_by_operator: str
    confirmation_phrase: str
    market_snapshot: dict[str, Any]
    inventory_snapshot: dict[str, Any]
    risk_snapshot: dict[str, Any]
    signal_snapshot: dict[str, Any]
    watchlist_snapshot: dict[str, Any]
    lot_size: int = 1
    order_type: str = "LIMIT"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PendingRealOrderApproval":
        required = {
            "approval_id",
            "signal_id",
            "symbol",
            "side",
            "role",
            "quantity",
            "limit_price",
            "expected_edge_bps",
            "created_at",
            "expires_at",
            "approved",
            "approved_by_operator",
            "confirmation_phrase",
            "market_snapshot",
            "inventory_snapshot",
            "risk_snapshot",
        }
        missing = sorted(key for key in required if key not in payload)
        if missing:
            raise ExecutionValidationError(f"approval missing required field(s): {', '.join(missing)}")

        return cls(
            approval_id=str(payload["approval_id"]).strip(),
            signal_id=str(payload["signal_id"]).strip(),
            symbol=_normalize_symbol(str(payload["symbol"])),
            side=OrderSide(str(payload["side"]).strip().upper()),
            role=OrderRole(str(payload["role"]).strip().upper()),
            quantity=int(payload["quantity"]),
            limit_price=_decimal(payload["limit_price"]),
            expected_edge_bps=_decimal(payload["expected_edge_bps"]),
            created_at=_parse_datetime(str(payload["created_at"])),
            expires_at=_parse_datetime(str(payload["expires_at"])),
            approved=_bool(payload["approved"]),
            approved_by_operator=str(payload["approved_by_operator"]).strip(),
            confirmation_phrase=str(payload["confirmation_phrase"]),
            market_snapshot=dict(payload["market_snapshot"]),
            inventory_snapshot=dict(payload["inventory_snapshot"]),
            risk_snapshot=dict(payload["risk_snapshot"]),
            signal_snapshot=dict(payload.get("signal_snapshot") or {}),
            watchlist_snapshot=dict(payload.get("watchlist_snapshot") or {}),
            lot_size=int(payload.get("lot_size") or 1),
            order_type=str(payload.get("order_type", "LIMIT")).strip().upper(),
        )

    @property
    def notional(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)


class ApprovedRealOrder(PendingRealOrderApproval):
    """Approval that has passed submit-time checks."""


@dataclass(frozen=True, slots=True)
class RejectedRealOrder:
    approval_id: str
    reason: str


def load_approval_file(path: str | Path) -> PendingRealOrderApproval:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ExecutionValidationError("approval file must contain one JSON object")
    return PendingRealOrderApproval.from_dict(payload)


def validate_approval(
    approval: PendingRealOrderApproval,
    *,
    now: datetime | None = None,
    confirmation_phrase: str = DEFAULT_CONFIRMATION_PHRASE,
    kill_switch_file: Path | None = None,
    require_approved: bool = False,
) -> ApprovedRealOrder | PendingRealOrderApproval:
    current = now or datetime.now(UTC)
    errors = approval_validation_errors(
        approval,
        now=current,
        confirmation_phrase=confirmation_phrase,
        kill_switch_file=kill_switch_file,
        require_approved=require_approved,
    )
    if errors:
        raise ExecutionValidationError("; ".join(errors))
    if require_approved:
        return ApprovedRealOrder(**{field.name: getattr(approval, field.name) for field in fields(PendingRealOrderApproval)})
    return approval


def approval_validation_errors(
    approval: PendingRealOrderApproval,
    *,
    now: datetime | None = None,
    confirmation_phrase: str = DEFAULT_CONFIRMATION_PHRASE,
    kill_switch_file: Path | None = None,
    require_approved: bool = False,
) -> list[str]:
    current = now or datetime.now(UTC)
    errors: list[str] = []

    if not approval.approval_id:
        errors.append("approval_id is required")
    if not approval.signal_id:
        errors.append("signal_id is required")
    if approval.quantity <= 0:
        errors.append("quantity must be positive")
    if approval.limit_price <= 0:
        errors.append("limit_price must be positive")
    if approval.lot_size <= 0 or approval.quantity % approval.lot_size != 0:
        errors.append("quantity must be lot-aligned")
    if approval.order_type not in LIMIT_ORDER_TYPES:
        errors.append("approval order_type must be limit-only")
    if approval.expires_at <= current:
        errors.append("approval is expired")
    if approval.created_at >= approval.expires_at:
        errors.append("approval created_at must be before expires_at")
    if approval.confirmation_phrase != confirmation_phrase:
        errors.append("confirmation phrase is incorrect")
    if kill_switch_file is not None and kill_switch_file.exists():
        errors.append("kill switch is active")

    if require_approved:
        if not approval.approved:
            errors.append("approval approved=false")
        if not approval.approved_by_operator:
            errors.append("approved_by_operator is required")

    if approval.side is OrderSide.SELL and approval.role is not OrderRole.TRADING_SELL:
        errors.append("sell role must be TRADING_SELL")
    if approval.side is OrderSide.BUY and approval.role is not OrderRole.TRADING_REBUY:
        errors.append("buy role must be TRADING_REBUY")

    if bool(approval.market_snapshot.get("stale")):
        errors.append("market snapshot is stale")
    if _decimal_or_none(approval.market_snapshot.get("spread_bps")) is None:
        errors.append("market snapshot requires spread_bps")
    if _decimal_or_none(approval.market_snapshot.get("max_spread_bps")) is None:
        errors.append("market snapshot requires max_spread_bps")
    if approval.side is OrderSide.SELL and _decimal_or_none(approval.market_snapshot.get("best_bid")) is None:
        errors.append("sell approval requires best_bid")
    if approval.side is OrderSide.BUY and _decimal_or_none(approval.market_snapshot.get("best_ask")) is None:
        errors.append("buy approval requires best_ask")

    if bool(approval.risk_snapshot.get("has_critical")):
        errors.append("critical risk blocks approval")
    if str(approval.risk_snapshot.get("max_severity", "")).strip().upper() == "CRITICAL":
        errors.append("critical risk blocks approval")

    if _watchlist_disabled(approval):
        errors.append("watchlist symbol is disabled")

    _append_signal_mismatch_errors(approval, errors)
    _append_inventory_errors(approval, errors)
    return errors


def approval_to_intent(approval: PendingRealOrderApproval) -> RealOrderIntent:
    return RealOrderIntent(
        symbol=approval.symbol,
        side=approval.side,
        quantity=approval.quantity,
        limit_price=approval.limit_price,
        role=approval.role,
        source=OrderSource.MANUAL,
        remark=f"approval:{approval.approval_id}",
        client_intent_id=approval.approval_id,
    )


def _append_signal_mismatch_errors(
    approval: PendingRealOrderApproval,
    errors: list[str],
) -> None:
    if not approval.signal_snapshot:
        return
    comparisons = {
        "symbol": approval.symbol,
        "side": approval.side.value,
        "role": approval.role.value,
        "quantity": approval.quantity,
        "limit_price": approval.limit_price,
    }
    for key, expected in comparisons.items():
        if key not in approval.signal_snapshot:
            continue
        actual = approval.signal_snapshot[key]
        if key == "symbol":
            matches = _normalize_symbol(str(actual)) == expected
        elif key == "limit_price":
            matches = _decimal(actual) == expected
        elif key == "quantity":
            matches = int(actual) == expected
        else:
            matches = str(actual).strip().upper() == str(expected)
        if not matches:
            errors.append(f"approval {key} differs from signal snapshot")


def _append_inventory_errors(
    approval: PendingRealOrderApproval,
    errors: list[str],
) -> None:
    sellable = _int_or_none(approval.inventory_snapshot.get("trading_available_to_sell"))
    rebuyable = _int_or_none(approval.inventory_snapshot.get("trading_available_to_rebuy"))
    if sellable is None:
        errors.append("inventory snapshot requires trading_available_to_sell")
    if rebuyable is None:
        errors.append("inventory snapshot requires trading_available_to_rebuy")
    if approval.role is OrderRole.TRADING_SELL and sellable is not None and approval.quantity > sellable:
        errors.append("sell quantity exceeds trading inventory")
    if approval.role is OrderRole.TRADING_REBUY and rebuyable is not None and approval.quantity > rebuyable:
        errors.append("rebuy quantity exceeds sold trading inventory")


def _watchlist_disabled(approval: PendingRealOrderApproval) -> bool:
    if approval.watchlist_snapshot:
        enabled = approval.watchlist_snapshot.get("enabled")
        if enabled is not None:
            return not _bool(enabled)
    enabled = approval.market_snapshot.get("watchlist_enabled")
    if enabled is not None:
        return not _bool(enabled)
    return False


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        return f"HK.{normalized}"
    return normalized


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return _decimal(value)


def _int_or_none(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)
