"""Manual approval files for guarded real orders."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
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
BLOCKED_SOURCE_SIGNAL_STATUSES = {"RISK_BLOCKED", "NOT_EXECUTABLE"}


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
    source_signal_status: str
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
            "source_signal_status",
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
            source_signal_status=str(payload["source_signal_status"]).strip().upper(),
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
    if require_approved:
        return validate_approval_for_submit(
            approval,
            now=now,
            confirmation_phrase=confirmation_phrase,
            kill_switch_file=kill_switch_file,
        )
    return validate_approval_static(approval)


def validate_approval_static(
    approval: PendingRealOrderApproval,
) -> PendingRealOrderApproval:
    errors = approval_static_validation_errors(approval)
    if errors:
        raise ExecutionValidationError("; ".join(errors))
    return approval


def validate_approval_for_submit(
    approval: PendingRealOrderApproval,
    *,
    now: datetime | None = None,
    confirmation_phrase: str = DEFAULT_CONFIRMATION_PHRASE,
    kill_switch_file: Path | None = None,
) -> ApprovedRealOrder:
    errors = approval_submit_validation_errors(
        approval,
        now=now,
        confirmation_phrase=confirmation_phrase,
        kill_switch_file=kill_switch_file,
    )
    if errors:
        raise ExecutionValidationError("; ".join(errors))
    return ApprovedRealOrder(**{field.name: getattr(approval, field.name) for field in fields(PendingRealOrderApproval)})


def approval_validation_errors(
    approval: PendingRealOrderApproval,
    *,
    now: datetime | None = None,
    confirmation_phrase: str = DEFAULT_CONFIRMATION_PHRASE,
    kill_switch_file: Path | None = None,
    require_approved: bool = False,
) -> list[str]:
    if require_approved:
        return approval_submit_validation_errors(
            approval,
            now=now,
            confirmation_phrase=confirmation_phrase,
            kill_switch_file=kill_switch_file,
        )
    return approval_static_validation_errors(approval)


def approval_static_validation_errors(
    approval: PendingRealOrderApproval,
) -> list[str]:
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
    if approval.created_at >= approval.expires_at:
        errors.append("approval created_at must be before expires_at")
    if not approval.source_signal_status:
        errors.append("source_signal_status is required")
    if approval.source_signal_status in BLOCKED_SOURCE_SIGNAL_STATUSES:
        errors.append(f"source signal status {approval.source_signal_status} cannot be approved")

    if approval.side is OrderSide.SELL and approval.role is not OrderRole.TRADING_SELL:
        errors.append("sell role must be TRADING_SELL")
    if approval.side is OrderSide.BUY and approval.role is not OrderRole.TRADING_REBUY:
        errors.append("buy role must be TRADING_REBUY")

    if _bool(approval.market_snapshot.get("stale")):
        errors.append("market snapshot is stale")
    if _decimal_or_none(approval.market_snapshot.get("spread_bps")) is None:
        errors.append("market snapshot requires spread_bps")
    if _decimal_or_none(approval.market_snapshot.get("max_spread_bps")) is None:
        errors.append("market snapshot requires max_spread_bps")
    if approval.side is OrderSide.SELL and _decimal_or_none(approval.market_snapshot.get("best_bid")) is None:
        errors.append("sell approval requires best_bid")
    if approval.side is OrderSide.BUY and _decimal_or_none(approval.market_snapshot.get("best_ask")) is None:
        errors.append("buy approval requires best_ask")

    if _bool(approval.risk_snapshot.get("has_critical")):
        errors.append("critical risk blocks approval")
    if str(approval.risk_snapshot.get("max_severity", "")).strip().upper() == "CRITICAL":
        errors.append("critical risk blocks approval")

    if _watchlist_disabled(approval):
        errors.append("watchlist symbol is disabled")

    _append_signal_mismatch_errors(approval, errors)
    _append_inventory_errors(approval, errors)
    return errors


def approval_submit_validation_errors(
    approval: PendingRealOrderApproval,
    *,
    now: datetime | None = None,
    confirmation_phrase: str = DEFAULT_CONFIRMATION_PHRASE,
    kill_switch_file: Path | None = None,
) -> list[str]:
    current = now or datetime.now(UTC)
    errors = approval_static_validation_errors(approval)
    if approval.expires_at <= current:
        errors.append("approval is expired")
    if approval.confirmation_phrase != confirmation_phrase:
        errors.append("confirmation phrase is incorrect")
    if kill_switch_file is not None and kill_switch_file.exists():
        errors.append("kill switch is active")
    if not approval.approved:
        errors.append("approval approved=false")
    if not approval.approved_by_operator:
        errors.append("approved_by_operator is required")
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


def draft_approval_from_strategy_signal(
    signal: dict[str, Any],
    *,
    now: datetime | None = None,
    expires_minutes: int = 5,
    max_spread_bps: Decimal | str | int | float = Decimal("20"),
) -> dict[str, Any]:
    if signal.get("event") != "strategy_signal":
        raise ExecutionValidationError("source row must be a strategy_signal event")
    status = str(signal.get("status") or _payload(signal).get("status") or "").strip().upper()
    if not status:
        raise ExecutionValidationError("strategy_signal missing status")
    if status in BLOCKED_SOURCE_SIGNAL_STATUSES:
        raise ExecutionValidationError(f"source signal status {status} cannot be drafted")

    side = str(signal.get("side") or _payload(signal).get("side") or "").strip().upper()
    role = str(signal.get("role") or _payload(signal).get("role") or "").strip().upper()
    quantity = int(signal.get("quantity") or _payload(signal).get("quantity") or 0)
    limit_price = _decimal(signal.get("limit_price") or _payload(signal).get("limit_price") or "0")
    if not side or not role or quantity <= 0 or limit_price <= 0:
        raise ExecutionValidationError("strategy_signal is not executable")

    current = now or datetime.now(UTC)
    expires = current + timedelta(minutes=expires_minutes)
    signal_id = str(signal.get("signal_id") or signal.get("client_intent_id") or "").strip()
    if not signal_id:
        raise ExecutionValidationError("strategy_signal missing signal_id")
    symbol = str(signal.get("symbol") or _payload(signal).get("symbol") or "").strip()
    if not symbol:
        raise ExecutionValidationError("strategy_signal missing symbol")

    market_snapshot = dict(signal.get("market_snapshot") or _payload(signal).get("market_snapshot") or {})
    market_snapshot.setdefault("stale", False)
    market_snapshot.setdefault("max_spread_bps", str(max_spread_bps))
    if side == "SELL":
        market_snapshot.setdefault("best_bid", str(limit_price))
    if side == "BUY":
        market_snapshot.setdefault("best_ask", str(limit_price))

    risk_snapshot = dict(signal.get("risk_snapshot") or _payload(signal).get("risk_snapshot") or {})
    if not risk_snapshot:
        risk_snapshot = {
            "max_severity": "UNKNOWN",
            "has_critical": False,
            "source": "strategy_signal_without_risk_snapshot",
        }

    return {
        "approval_id": f"draft-{signal_id}",
        "signal_id": signal_id,
        "symbol": _normalize_symbol(symbol),
        "side": side,
        "role": role,
        "quantity": quantity,
        "limit_price": str(limit_price),
        "expected_edge_bps": str(signal.get("expected_edge_bps") or _payload(signal).get("expected_edge_bps") or "0"),
        "created_at": current.isoformat(),
        "expires_at": expires.isoformat(),
        "approved": False,
        "approved_by_operator": "",
        "confirmation_phrase": "",
        "source_signal_status": status,
        "lot_size": int(signal.get("lot_size") or _payload(signal).get("lot_size") or 1),
        "order_type": "LIMIT",
        "market_snapshot": _jsonable(market_snapshot),
        "inventory_snapshot": _jsonable(dict(signal.get("inventory_snapshot") or _payload(signal).get("inventory_snapshot") or {})),
        "risk_snapshot": _jsonable(risk_snapshot),
        "signal_snapshot": _jsonable(signal),
    }


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


def _payload(signal: dict[str, Any]) -> dict[str, Any]:
    payload = signal.get("payload")
    return payload if isinstance(payload, dict) else {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


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
