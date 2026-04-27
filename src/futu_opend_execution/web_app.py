"""Local Web UI server for Futu OpenD execution.

The server intentionally uses the Python standard library so the execution
console can run without a frontend build step.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from futu_opend_execution._compat import UTC
from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import BrokerError
from futu_opend_execution.grey_open import (
    FutuGreyMarketOpenDClient,
    GreyMarketOpenTrigger,
    GreyMarketTriggerRules,
    JsonlEventLogger,
    log_signal,
    run_replay,
)
from futu_opend_execution.inventory import split_inventory_targets
from futu_opend_execution.normal_trade import (
    FutuNormalTradeClient,
    NormalOrderType,
    NormalQuantityMode,
    NormalTradeSide,
    build_normal_trade_intent,
    load_dotenv,
)
from futu_opend_execution.risk import ExecutionValidationError, validate_runtime_config
from futu_opend_execution.services.cost_reducer import (
    CostReducerAction,
    CostReducerDecision,
    CostReducerEngine,
    CostReducerExecutionPolicy,
    CostReducerRules,
    CostReducerState,
    build_executable_intent,
)
from futu_opend_execution.services.real_order import (
    GreyMarketRealOrderIntent,
    GreyOrderRole,
    GreyOrderSide,
    GreyOrderSource,
    RealOrderGuard,
)
from futu_opend_execution.services.reconciliation import (
    FillRecord,
    InventoryManager,
    PositionReconciler,
)
from futu_opend_execution.signals.intraday_adaptive import AdaptiveMarketState
from futu_opend_execution.strategy_config import (
    CostReducerRuntimeParams,
    ExecutionMode,
    WebUiRuntimeState,
    config_to_jsonable,
    cost_reducer_preset,
    update_cost_reducer_params,
)


STATIC_ROOT = Path(__file__).with_name("web_static")
DEFAULT_KILL_SWITCH = Path("/tmp/futu-opend-execution.KILL")
REAL_CONFIRM_TEXT = "确认实盘"
REAL_DUPLICATE_WINDOW_SECONDS = 3.0


class WebState:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        log_file: Path,
        kill_switch_file: Path,
    ) -> None:
        self.config = config
        self.log_file = log_file
        self.kill_switch_file = kill_switch_file
        self._lock = Lock()
        self._last_real_signature: str | None = None
        self._last_real_time = 0.0
        self.ui_state = WebUiRuntimeState()
        self.cost_reducer_params = CostReducerRuntimeParams()
        self.inventory_manager: InventoryManager | None = None
        self.pending_cost_reducer_intents: dict[str, dict[str, Any]] = {}
        self.order_records: list[dict[str, Any]] = []
        self.fill_records: list[dict[str, Any]] = []
        self.replay_summary: dict[str, Any] | None = None
        self.latest_market_state: dict[str, Any] | None = None
        self.latest_decision: dict[str, Any] | None = None

    def check_duplicate_real_order(self, signature: str) -> None:
        now = time.monotonic()
        with self._lock:
            if (
                self._last_real_signature == signature
                and now - self._last_real_time < REAL_DUPLICATE_WINDOW_SECONDS
            ):
                raise ExecutionValidationError(
                    "同一真实订单刚刚提交过，已拦截重复点击。"
                )
            self._last_real_signature = signature
            self._last_real_time = now


def build_app_handler(state: WebState):
    class FutuWebHandler(BaseHTTPRequestHandler):
        server_version = "FutuExecutionWeb/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                query = parse_qs(parsed.query)
                self._handle_json(
                    lambda: api_health(
                        state,
                        active=_first_query_value(query, "active", "0"),
                        symbol=_first_query_value(query, "symbol", ""),
                    )
                )
                return
            if parsed.path == "/api/quote":
                query = parse_qs(parsed.query)
                self._handle_json(
                    lambda: api_quote(
                        state,
                        symbol=_first_query_value(query, "symbol"),
                    )
                )
                return
            if parsed.path == "/api/events":
                query = parse_qs(parsed.query)
                self._handle_json(
                    lambda: api_events(
                        state,
                        limit=int(_first_query_value(query, "limit", "80")),
                    )
                )
                return
            if parsed.path == "/api/state":
                self._handle_json(lambda: api_state(state))
                return
            if parsed.path == "/api/cost-reducer/config":
                self._handle_json(lambda: api_cost_reducer_config(state))
                return
            if parsed.path == "/api/inventory":
                self._handle_json(lambda: api_inventory(state))
                return
            if parsed.path == "/api/orders":
                self._handle_json(lambda: {"orders": state.order_records})
                return
            if parsed.path == "/api/fills":
                self._handle_json(lambda: {"fills": state.fill_records})
                return
            if parsed.path == "/api/replay/summary":
                self._handle_json(lambda: {"summary": state.replay_summary})
                return
            if parsed.path == "/api/logs/tail":
                query = parse_qs(parsed.query)
                self._handle_json(
                    lambda: api_logs_tail(
                        state,
                        limit=int(_first_query_value(query, "limit", "80")),
                    )
                )
                return
            if parsed.path == "/api/logs/filter":
                query = parse_qs(parsed.query)
                self._handle_json(
                    lambda: api_logs_filter(
                        state,
                        event_type=_first_query_value(query, "event", ""),
                        text=_first_query_value(query, "text", ""),
                        limit=int(_first_query_value(query, "limit", "200")),
                    )
                )
                return
            self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/normal/order":
                self._handle_json(lambda: api_normal_order(state, self._read_json()))
                return
            if parsed.path == "/api/grey/evaluate":
                self._handle_json(lambda: api_grey_evaluate(state, self._read_json()))
                return
            if parsed.path == "/api/kill-switch":
                self._handle_json(lambda: api_kill_switch(state, self._read_json()))
                return
            if parsed.path == "/api/validate-config":
                self._handle_json(lambda: api_validate_config(state, self._read_json()))
                return
            if parsed.path == "/api/subscribe":
                self._handle_json(lambda: api_subscribe(state, self._read_json()))
                return
            if parsed.path == "/api/grey-open/dry-run":
                self._handle_json(lambda: api_grey_evaluate(state, self._read_json()))
                return
            if parsed.path == "/api/grey-open/start-live-dry-run":
                self._handle_json(lambda: api_start_live_dry_run(state, self._read_json()))
                return
            if parsed.path == "/api/grey-open/stop":
                self._handle_json(lambda: api_stop_live_run(state))
                return
            if parsed.path == "/api/kill-switch/create":
                self._handle_json(lambda: api_kill_switch(state, {"enabled": True}))
                return
            if parsed.path == "/api/kill-switch/clear":
                self._handle_json(lambda: api_kill_switch(state, {"enabled": False}))
                return
            if parsed.path == "/api/cost-reducer/config":
                self._handle_json(lambda: api_update_cost_reducer_config(state, self._read_json()))
                return
            if parsed.path == "/api/cost-reducer/evaluate":
                self._handle_json(lambda: api_cost_reducer_evaluate(state, self._read_json()))
                return
            if parsed.path == "/api/cost-reducer/approve-intent":
                self._handle_json(lambda: api_approve_cost_reducer_intent(state, self._read_json()))
                return
            if parsed.path == "/api/cost-reducer/reject-intent":
                self._handle_json(lambda: api_mark_cost_reducer_intent(state, self._read_json(), "REJECTED"))
                return
            if parsed.path == "/api/cost-reducer/expire-intent":
                self._handle_json(lambda: api_mark_cost_reducer_intent(state, self._read_json(), "EXPIRED"))
                return
            if parsed.path == "/api/inventory/seed-dry-run":
                self._handle_json(lambda: api_inventory_seed_dry_run(state, self._read_json()))
                return
            if parsed.path == "/api/inventory/reset":
                self._handle_json(lambda: api_inventory_reset(state))
                return
            if parsed.path == "/api/inventory/reconcile":
                self._handle_json(lambda: api_inventory_reconcile(state))
                return
            if parsed.path == "/api/orders/cancel":
                self._handle_json(lambda: api_order_cancel(state, self._read_json()))
                return
            if parsed.path == "/api/replay/run":
                self._handle_json(lambda: api_replay_run(state, self._read_json()))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ExecutionValidationError("请求体必须是 JSON object。")
            return payload

        def _handle_json(self, fn) -> None:
            try:
                payload = fn()
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
            except (ExecutionValidationError, BrokerError, ValueError) as exc:
                _log_event(
                    state,
                    "web_error",
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _log_event(
                    state,
                    "web_error",
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": "操作失败，状态未知，请到 OpenD 或券商端核对订单。",
                        "error_type": type(exc).__name__,
                    },
                )

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, path: str) -> None:
            requested = "index.html" if path in {"", "/"} else path.lstrip("/")
            target = (STATIC_ROOT / requested).resolve()
            if not _is_relative_to(target, STATIC_ROOT.resolve()) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return FutuWebHandler


def api_health(
    state: WebState,
    *,
    active: bool | str = False,
    symbol: str = "",
) -> dict[str, Any]:
    validate_runtime_config(state.config)
    active_enabled = _truthy(active)
    payload: dict[str, Any] = {
        "status": "READY",
        "host": state.config.futu_host,
        "port": state.config.futu_port,
        "allow_real_trade": state.config.allow_real_trade,
        "kill_switch": state.kill_switch_file.exists(),
        "kill_switch_file": str(state.kill_switch_file),
        "log_file": str(state.log_file),
    }
    if not active_enabled:
        return payload

    probe_symbol = symbol.strip() or "HK.00700"
    started = time.monotonic()
    try:
        with FutuNormalTradeClient(state.config) as client:
            quote = client.read_quote(probe_symbol)
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "DEGRADED"
        payload["opend_quote_probe"] = {
            "ok": False,
            "symbol": probe_symbol,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
        return payload

    payload["opend_quote_probe"] = {
        "ok": True,
        "symbol": quote.symbol,
        "best_bid": quote.best_bid,
        "best_ask": quote.best_ask,
        "last_price": quote.last_price,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }
    return payload


def api_quote(state: WebState, *, symbol: str) -> dict[str, Any]:
    if not symbol:
        raise ExecutionValidationError("请输入标的代码。")
    with FutuNormalTradeClient(state.config) as client:
        quote = client.read_quote(symbol)
    payload = _quote_to_payload(quote)
    _log_event(state, "web_quote", quote=payload)
    return {"quote": payload}


def api_state(state: WebState) -> dict[str, Any]:
    return {
        "ui_state": config_to_jsonable(state.ui_state),
        "cost_reducer_config": config_to_jsonable(state.cost_reducer_params),
        "inventory": _inventory_payload(state),
        "pending_cost_reducer_intents": list(state.pending_cost_reducer_intents.values()),
        "orders": state.order_records[-50:],
        "fills": state.fill_records[-50:],
        "latest_market_state": state.latest_market_state,
        "latest_decision": state.latest_decision,
        "kill_switch": state.kill_switch_file.exists(),
        "allow_real_trade": state.config.allow_real_trade,
        "log_file": str(state.log_file),
    }


def api_validate_config(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    rules = _rules_from_payload(state, payload)
    validate_runtime_config(state.config)
    return {
        "valid": True,
        "rules": _dataclass_to_payload(rules),
        "cost_reducer_config": config_to_jsonable(state.cost_reducer_params),
        "safety": {
            "default_dry_run": True,
            "allow_real_trade": state.config.allow_real_trade,
            "kill_switch": state.kill_switch_file.exists(),
            "kill_switch_file": str(state.kill_switch_file),
        },
    }


def api_subscribe(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    symbol = _required_str(payload, "symbol")
    active = _truthy(payload.get("active", False))
    if active:
        with FutuGreyMarketOpenDClient(state.config) as client:
            client.subscribe_market(symbol)
    state.ui_state.active_symbol = _normalize_symbol(symbol)
    _log_event(state, "web_subscribe", symbol=state.ui_state.active_symbol, active=active)
    return {"subscribed": active, "symbol": state.ui_state.active_symbol}


def api_start_live_dry_run(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    _rules_from_payload(state, payload)
    state.ui_state.execution_mode = ExecutionMode.LIVE_DRY_RUN
    state.ui_state.live_running = True
    _log_event(state, "web_live_dry_run_started", payload=payload)
    return {"running": True, "execution_mode": state.ui_state.execution_mode.value}


def api_stop_live_run(state: WebState) -> dict[str, Any]:
    state.ui_state.live_running = False
    _log_event(state, "web_live_run_stopped")
    return {"running": False}


def api_cost_reducer_config(state: WebState) -> dict[str, Any]:
    return {"config": config_to_jsonable(state.cost_reducer_params)}


def api_update_cost_reducer_config(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    preset = payload.get("preset")
    if preset:
        state.cost_reducer_params = cost_reducer_preset(str(preset))
    else:
        state.cost_reducer_params = update_cost_reducer_params(
            state.cost_reducer_params,
            payload,
        )
    _log_event(
        state,
        "web_cost_reducer_config_updated",
        config=config_to_jsonable(state.cost_reducer_params),
    )
    return {"config": config_to_jsonable(state.cost_reducer_params)}


def api_inventory(state: WebState) -> dict[str, Any]:
    return {"inventory": _inventory_payload(state)}


def api_inventory_seed_dry_run(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    total_quantity = _required_int(payload, "target_quantity")
    lot_size = _required_int(payload, "lot_size")
    anchor_price = _required_str(payload, "anchor_price")
    inventory = split_inventory_targets(
        total_quantity=total_quantity,
        lot_size=lot_size,
        core_ratio=payload.get("core_ratio", state.cost_reducer_params.core_ratio),
        trading_ratio=payload.get("trading_ratio", state.cost_reducer_params.trading_ratio),
    )
    inventory.seed_opening_inventory(anchor_price=anchor_price)
    state.inventory_manager = InventoryManager(inventory)
    _log_event(state, "web_inventory_seeded", inventory=state.inventory_manager.snapshot())
    return {"inventory": state.inventory_manager.snapshot()}


def api_inventory_reset(state: WebState) -> dict[str, Any]:
    state.inventory_manager = None
    state.pending_cost_reducer_intents.clear()
    _log_event(state, "web_inventory_reset")
    return {"inventory": None}


def api_inventory_reconcile(state: WebState) -> dict[str, Any]:
    if state.inventory_manager is None:
        raise ExecutionValidationError("inventory has not been seeded.")
    payload = PositionReconciler().reconcile(state.inventory_manager)
    _log_event(state, payload["event"], **payload)
    return payload


def api_cost_reducer_evaluate(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    manager = state.inventory_manager
    if manager is None:
        raise ExecutionValidationError("inventory must be seeded before cost reducer evaluation.")
    market = _market_from_payload(payload)
    params = state.cost_reducer_params
    rules = _cost_rules_from_params(params)
    reducer_state = CostReducerState(
        round_trips_completed=int(payload.get("round_trips_completed") or 0),
        last_sell_price=(
            Decimal(str(payload["last_sell_price"]))
            if payload.get("last_sell_price") not in {None, ""}
            else None
        ),
    )
    decision = CostReducerEngine(rules).evaluate(
        inventory=manager.inventory,
        market=market,
        state=reducer_state,
    )
    executable = build_executable_intent(
        decision=decision,
        market=market,
        inventory=manager.inventory,
        rules=rules,
        policy=_execution_policy_from_params(params),
        best_bid=payload.get("best_bid"),
        best_ask=payload.get("best_ask"),
        last_sell_price=reducer_state.last_sell_price,
    )
    intent_payload = _dataclass_to_payload(executable)
    if decision.action in {CostReducerAction.SELL_TRADING, CostReducerAction.REBUY_TRADING}:
        intent_id = f"cr-{int(time.time() * 1000)}"
        intent_payload["intent_id"] = intent_id
        intent_payload["created_at"] = datetime.now(UTC).isoformat()
        state.pending_cost_reducer_intents[intent_id] = intent_payload
    state.latest_market_state = _dataclass_to_payload(market)
    state.latest_decision = {
        "decision": _dataclass_to_payload(decision),
        "executable_intent": intent_payload,
    }
    _log_event(state, "web_cost_reducer_evaluate", **state.latest_decision)
    return state.latest_decision


def api_approve_cost_reducer_intent(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    intent_id = str(payload.get("intent_id") or "").strip()
    if not intent_id:
        _raise_blocked_real_order(state, "intent_id is required", payload=payload)
    pending = state.pending_cost_reducer_intents.get(intent_id)
    if pending is None:
        _raise_blocked_real_order(state, "pending intent not found", intent_id=intent_id)
    if not _truthy(payload.get("acknowledge_real_order", False)):
        _raise_blocked_real_order(
            state,
            "real order acknowledgement checkbox is required",
            intent_id=intent_id,
        )
    if not _truthy(payload.get("real_mode", False)):
        _raise_blocked_real_order(
            state,
            "real mode must be toggled before approval",
            intent_id=intent_id,
        )
    if str(payload.get("confirm_text") or "") != REAL_CONFIRM_TEXT:
        _raise_blocked_real_order(
            state,
            f"real orders require confirmation phrase: {REAL_CONFIRM_TEXT}",
            intent_id=intent_id,
        )
    if state.inventory_manager is None:
        _raise_blocked_real_order(state, "inventory is required before approval", intent_id=intent_id)

    side = pending.get("side")
    role = pending.get("role")
    if not side or not role or not pending.get("limit_price"):
        _raise_blocked_real_order(state, "intent is not executable", intent_id=intent_id, pending=pending)
    real_intent = GreyMarketRealOrderIntent(
        symbol=str(payload.get("symbol") or state.ui_state.active_symbol),
        side=side,
        quantity=int(pending["quantity"]),
        limit_price=pending["limit_price"],
        role=role,
        source=GreyOrderSource.COST_REDUCER,
        remark=str(payload.get("remark") or "web_cost_reducer_manual"),
        client_intent_id=intent_id,
    )
    guard = RealOrderGuard(
        runtime_config=state.config,
        kill_switch_file=state.kill_switch_file,
        max_qty=int(payload.get("max_qty") or real_intent.quantity),
        max_notional=Decimal(str(payload.get("max_notional") or real_intent.notional)),
        lot_size=int(payload.get("lot_size") or 1),
        max_order_attempts=int(payload.get("max_order_attempts") or 1),
        experimental_auto_enabled=False,
    )
    market_snapshot = dict(pending.get("market_snapshot") or {})
    market_snapshot.setdefault("spread_bps", market_snapshot.get("spread_bps", "0"))
    market_snapshot.setdefault(
        "max_spread_bps",
        str(state.cost_reducer_params.max_spread_bps),
    )
    market_snapshot.setdefault("best_bid", payload.get("best_bid"))
    market_snapshot.setdefault("best_ask", payload.get("best_ask"))
    try:
        guard.validate(
            real_intent,
            execution_mode=ExecutionMode.LIVE_REAL_COST_REDUCER_MANUAL_APPROVAL,
            inventory=state.inventory_manager.inventory,
            market_snapshot=market_snapshot,
            confirm_text=str(payload.get("confirm_text") or ""),
            approved=True,
            now_monotonic=time.monotonic(),
        )
    except ExecutionValidationError as exc:
        _raise_blocked_real_order(
            state,
            str(exc),
            intent_id=intent_id,
            intent=real_intent,
            market_snapshot=market_snapshot,
        )
    if not _truthy(payload.get("submit_real", False)):
        pending["status"] = "PENDING_APPROVAL"
        _raise_blocked_real_order(
            state,
            "submit_real flag must be true to place a real order",
            intent_id=intent_id,
            intent=real_intent,
        )

    with FutuGreyMarketOpenDClient(state.config) as client:
        response = client.place_real_limit_order(real_intent)
    record = {
        "event": "real_order_response",
        "intent_id": intent_id,
        "intent": _dataclass_to_payload(real_intent),
        "response": response,
    }
    state.order_records.append(record)
    pending["status"] = "EXECUTED"
    _log_event(state, "real_order_response", **record)
    return {"submitted": True, "intent": _dataclass_to_payload(real_intent), "response": response}


def api_mark_cost_reducer_intent(
    state: WebState,
    payload: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    intent_id = _required_str(payload, "intent_id")
    pending = state.pending_cost_reducer_intents.get(intent_id)
    if pending is None:
        raise ExecutionValidationError("pending intent not found.")
    pending["status"] = status
    _log_event(state, "web_cost_reducer_intent_status", intent_id=intent_id, status=status)
    return {"intent": pending}


def api_order_cancel(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    order_id = _required_str(payload, "order_id")
    if state.inventory_manager is not None:
        event = state.inventory_manager.mark_cancelled(order_id)
    else:
        event = {"event": "real_order_cancel_request", "order_id": order_id}
    state.order_records.append(event)
    _log_event(state, event["event"], **event)
    return event


def api_replay_run(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    input_path = Path(_required_str(payload, "input_path"))
    output_log = Path(str(payload.get("output_log_path") or state.log_file))
    rules = _rules_from_payload(state, payload)
    with JsonlEventLogger(output_log) as logger:
        submitted = run_replay(
            input_path=input_path,
            rules=rules,
            logger=logger,
            stdout=_NullWriter(),
            cost_reducer_dry_run=_truthy(payload.get("cost_reducer_dry_run", True)),
            core_ratio=state.cost_reducer_params.core_ratio,
            trading_ratio=state.cost_reducer_params.trading_ratio,
            estimated_roundtrip_cost_bps=state.cost_reducer_params.estimated_roundtrip_cost_bps,
            safety_buffer_bps=state.cost_reducer_params.safety_buffer_bps,
            max_spread_bps=state.cost_reducer_params.max_spread_bps,
            min_turnover_to_activate=state.cost_reducer_params.min_turnover_to_activate,
            min_ticks_to_activate=state.cost_reducer_params.min_ticks_to_activate,
            overextension_vol_multiple=state.cost_reducer_params.overextension_vol_multiple,
            high_pullback_vol_multiple=state.cost_reducer_params.high_pullback_vol_multiple,
            rebuy_anchor_vol_band=state.cost_reducer_params.rebuy_anchor_vol_band,
            max_sell_total_position_ratio=state.cost_reducer_params.max_sell_total_position_ratio,
            max_round_trips=state.cost_reducer_params.max_round_trips,
        )
    summary = _latest_event(output_log, "cost_reducer_replay_summary")
    state.replay_summary = summary
    _log_event(state, "web_replay_run", submitted=submitted, summary=summary)
    return {"submitted_or_would_submit": submitted, "summary": summary, "output_log_path": str(output_log)}


def api_logs_tail(state: WebState, *, limit: int = 80) -> dict[str, Any]:
    limit = max(min(limit, 500), 1)
    return {"events": _read_jsonl_tail(state.log_file, limit)}


def api_logs_filter(
    state: WebState,
    *,
    event_type: str = "",
    text: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    events = _read_jsonl_tail(state.log_file, max(min(limit, 1000), 1))
    if event_type:
        events = [event for event in events if event.get("event") == event_type]
    if text:
        needle = text.lower()
        events = [event for event in events if needle in json.dumps(event, ensure_ascii=False).lower()]
    return {"events": events}


def _raise_blocked_real_order(
    state: WebState,
    reason: str,
    **payload: Any,
) -> None:
    _log_event(state, "blocked_real_order", reason=reason, **payload)
    raise ExecutionValidationError(reason)


def api_normal_order(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    symbol = _required_str(payload, "symbol")
    side = NormalTradeSide(_required_str(payload, "side").upper())
    order_type = NormalOrderType(_required_str(payload, "order_type").upper())
    quantity_mode = NormalQuantityMode(_required_str(payload, "quantity_mode").upper())
    real = bool(payload.get("real", False))
    limit_price = payload.get("limit_price") or None
    max_notional = _required_str(payload, "max_notional")
    lots = _optional_int(payload.get("lots"))
    shares = _optional_int(payload.get("shares"))
    remark = str(payload.get("remark") or "web_normal_trade")

    if state.kill_switch_file.exists():
        raise ExecutionValidationError("Kill switch 已开启，禁止下单。")
    if real:
        if not state.config.allow_real_trade:
            raise ExecutionValidationError("环境未开启 FUTU_ALLOW_REAL_TRADE=1。")
        if str(payload.get("confirm_text") or "") != REAL_CONFIRM_TEXT:
            raise ExecutionValidationError(f"实盘下单需要输入确认短语：{REAL_CONFIRM_TEXT}")

    with FutuNormalTradeClient(state.config) as client:
        quote = client.read_quote(symbol)
        intent = build_normal_trade_intent(
            quote=quote,
            side=side,
            order_type=order_type,
            quantity_mode=quantity_mode,
            lots=lots,
            shares=shares,
            limit_price=limit_price,
            max_notional=max_notional,
            remark=remark,
        )
        signature = _normal_order_signature(intent)
        if real:
            state.check_duplicate_real_order(signature)

        request_payload = {
            "dry_run": not real,
            "intent": _intent_to_payload(intent),
            "quote": _quote_to_payload(quote),
        }
        _log_event(state, "web_normal_order_request", **request_payload)
        if not real:
            return {
                "submitted": False,
                "dry_run": True,
                "intent": _intent_to_payload(intent),
                "quote": _quote_to_payload(quote),
            }

        response = client.place_order(intent)
        order_id = _extract_order_id(response)
        timeline = (
            client.wait_for_terminal_order(order_id=order_id, symbol=intent.symbol)
            if order_id
            else []
        )
        _log_event(
            state,
            "web_normal_order_response",
            response=response,
            timeline=timeline,
        )
        return {
            "submitted": True,
            "dry_run": False,
            "intent": _intent_to_payload(intent),
            "quote": _quote_to_payload(quote),
            "response": response,
            "timeline": timeline,
        }


def api_grey_evaluate(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    symbol = _required_str(payload, "symbol")
    max_price = _required_str(payload, "max_price")
    quantity = _required_int(payload, "quantity")
    max_notional = _required_str(payload, "max_notional")
    max_attempts = int(payload.get("max_order_attempts") or 3)
    cool_down_ms = int(payload.get("cool_down_ms") or 300)
    real = bool(payload.get("real", False))
    if real:
        raise ExecutionValidationError(
            "Web UI 当前只支持暗盘 dry-run 评估；实盘抢单请先保持人工确认。"
        )

    rules = GreyMarketTriggerRules(
        symbol=symbol,
        quantity=quantity,
        max_price=max_price,
        max_qty=quantity,
        max_notional=max_notional,
        max_order_attempts=max_attempts,
        cool_down_ms=cool_down_ms,
        kill_switch_file=state.kill_switch_file,
        remark="web_grey_open",
    )
    with JsonlEventLogger(state.log_file) as logger:
        with FutuGreyMarketOpenDClient(state.config) as client:
            client.subscribe_market(symbol)
            signal = client.read_signal(symbol)
            log_signal(logger, signal)
            decision = GreyMarketOpenTrigger(rules).evaluate(
                signal,
                now_monotonic=time.monotonic(),
            )
            logger.log(
                "web_grey_evaluate",
                action=decision.action.value,
                reason=decision.reason,
                signal=signal,
                intent=decision.intent,
            )
    return {
        "signal": _dataclass_to_payload(signal),
        "decision": {
            "action": decision.action.value,
            "reason": decision.reason,
            "intent": _dataclass_to_payload(decision.intent),
        },
    }


def api_kill_switch(state: WebState, payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", True))
    if enabled:
        state.kill_switch_file.write_text(
            f"enabled_at={datetime.now(UTC).isoformat()}\n",
            encoding="utf-8",
        )
    else:
        if state.kill_switch_file.exists():
            state.kill_switch_file.unlink()
    _log_event(state, "web_kill_switch", enabled=enabled)
    return {
        "kill_switch": state.kill_switch_file.exists(),
        "kill_switch_file": str(state.kill_switch_file),
    }


def api_events(state: WebState, *, limit: int = 80) -> dict[str, Any]:
    limit = max(min(limit, 500), 1)
    events: list[dict[str, Any]] = []
    state_log = state.log_file.resolve()
    for path in sorted(Path("logs").glob("*.jsonl")):
        if path.resolve() == state_log:
            continue
        events.extend(_read_jsonl_tail(path, limit))
    events.extend(_read_jsonl_tail(state.log_file, limit))
    events = sorted(events, key=lambda event: str(event.get("ts", "")))
    return {"events": events[-limit:]}


def _rules_from_payload(state: WebState, payload: dict[str, Any]) -> GreyMarketTriggerRules:
    symbol = _required_str(payload, "symbol")
    quantity = _required_int(payload, "quantity")
    max_price = _required_str(payload, "max_price")
    max_notional = _required_str(payload, "max_notional")
    max_qty = int(payload.get("max_qty") or quantity)
    return GreyMarketTriggerRules(
        symbol=symbol,
        quantity=quantity,
        max_price=max_price,
        max_qty=max_qty,
        max_notional=max_notional,
        max_order_attempts=int(payload.get("max_order_attempts") or 3),
        cool_down_ms=int(payload.get("cool_down_ms") or 300),
        opening_burst_seconds=float(payload.get("opening_burst_seconds") or 0),
        opening_burst_cool_down_ms=int(payload.get("opening_burst_cool_down_ms") or 50),
        kill_switch_file=state.kill_switch_file,
        remark=str(payload.get("remark") or "web_grey_open"),
    )


def _cost_rules_from_params(params: CostReducerRuntimeParams) -> CostReducerRules:
    return CostReducerRules(
        core_ratio=params.core_ratio,
        trading_ratio=params.trading_ratio,
        overextension_vol_multiple=params.overextension_vol_multiple,
        high_pullback_vol_multiple=params.high_pullback_vol_multiple,
        rebuy_anchor_vol_band=params.rebuy_anchor_vol_band,
        max_sell_total_position_ratio=params.max_sell_total_position_ratio,
        max_round_trips=params.max_round_trips,
        min_turnover_to_activate=params.min_turnover_to_activate,
        min_ticks_to_activate=params.min_ticks_to_activate,
        max_spread_bps=params.max_spread_bps,
        estimated_roundtrip_cost_bps=params.estimated_roundtrip_cost_bps,
        safety_buffer_bps=params.safety_buffer_bps,
    )


def _execution_policy_from_params(params: CostReducerRuntimeParams) -> CostReducerExecutionPolicy:
    return CostReducerExecutionPolicy(
        dry_run_only=params.dry_run_only,
        manual_approval_required=params.manual_approval_required,
        enable_real_sell=params.enable_real_sell,
        enable_real_rebuy=params.enable_real_rebuy,
        enable_auto_cost_reducer=params.enable_auto_cost_reducer,
        max_real_sell_qty=params.max_real_sell_qty,
        max_real_rebuy_qty=params.max_real_rebuy_qty,
        max_real_sell_notional=params.max_real_sell_notional,
        max_real_rebuy_notional=params.max_real_rebuy_notional,
        max_cost_reducer_orders_per_session=params.max_cost_reducer_orders_per_session,
        min_seconds_between_cost_reducer_orders=params.min_seconds_between_cost_reducer_orders,
        require_positive_expected_edge=params.require_positive_expected_edge,
        sell_limit_offset_ticks=params.sell_limit_offset_ticks,
        rebuy_limit_offset_ticks=params.rebuy_limit_offset_ticks,
        min_sell_price=params.min_sell_price,
        max_rebuy_price=params.max_rebuy_price,
        max_sell_slippage_bps=params.max_sell_slippage_bps,
        max_rebuy_slippage_bps=params.max_rebuy_slippage_bps,
        min_expected_edge_bps=params.min_expected_edge_bps,
    )


def _market_from_payload(payload: dict[str, Any]) -> AdaptiveMarketState:
    return AdaptiveMarketState(
        opening_vwap=_optional_decimal(payload.get("opening_vwap")),
        rolling_vwap=_optional_decimal(payload.get("rolling_vwap")),
        realized_vol=_optional_decimal(payload.get("realized_vol")) or Decimal("0"),
        rolling_high=_optional_decimal(payload.get("rolling_high")),
        rolling_low=_optional_decimal(payload.get("rolling_low")),
        cumulative_turnover=_optional_decimal(payload.get("cumulative_turnover")) or Decimal("0"),
        volume_delta=_optional_decimal(payload.get("volume_delta")) or Decimal("0"),
        turnover_delta=_optional_decimal(payload.get("turnover_delta")) or Decimal("0"),
        cumulative_field_reset_detected=_truthy(payload.get("cumulative_field_reset_detected", False)),
        tick_count=int(payload.get("tick_count") or 0),
        orderbook_imbalance=_optional_decimal(payload.get("orderbook_imbalance")) or Decimal("0"),
        spread_bps=_optional_decimal(payload.get("spread_bps")) or Decimal("0"),
        last_price=_optional_decimal(payload.get("last_price")),
    )


def _inventory_payload(state: WebState) -> dict[str, Any] | None:
    if state.inventory_manager is None:
        return None
    return state.inventory_manager.snapshot()


def _latest_event(path: Path, event_type: str) -> dict[str, Any] | None:
    events = _read_jsonl_tail(path, 1000)
    for event in reversed(events):
        if event.get("event") == event_type:
            return event
    return None


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        return normalized
    return f"HK.{normalized}"


class _NullWriter:
    def write(self, value: str) -> int:
        return len(value)


def run_server(*, host: str, port: int, state: WebState) -> None:
    handler = build_app_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Web UI: http://{host}:{port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local Futu execution Web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-file", type=Path, default=Path("logs/web_ui.jsonl"))
    parser.add_argument(
        "--kill-switch-file",
        type=Path,
        default=DEFAULT_KILL_SWITCH,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    state = WebState(
        config=RuntimeConfig.from_env(),
        log_file=args.log_file,
        kill_switch_file=args.kill_switch_file,
    )
    run_server(host=args.host, port=args.port, state=state)
    return 0


def _log_event(state: WebState, event_type: str, **payload: Any) -> None:
    with JsonlEventLogger(state.log_file) as logger:
        logger.log(event_type, **_sanitize(payload))


def _quote_to_payload(quote) -> dict[str, Any]:
    return {
        "symbol": quote.symbol,
        "lot_size": quote.lot_size,
        "best_bid": quote.best_bid,
        "bid": quote.best_bid,
        "best_ask": quote.best_ask,
        "ask": quote.best_ask,
        "last_price": quote.last_price,
        "price": quote.last_price,
        "name": (quote.raw_quote or {}).get("name") or (quote.raw_basic or {}).get("name"),
    }


def _intent_to_payload(intent) -> dict[str, Any]:
    return {
        "symbol": intent.symbol,
        "side": intent.side.value,
        "order_type": intent.order_type.value,
        "quantity": intent.quantity,
        "price": intent.broker_price,
        "limit_price": intent.limit_price,
        "risk_price": intent.risk_price,
        "risk_notional": intent.notional,
        "max_notional": intent.max_notional,
        "remark": intent.remark,
    }


def _normal_order_signature(intent) -> str:
    return "|".join(
        [
            intent.symbol,
            intent.side.value,
            intent.order_type.value,
            str(intent.quantity),
            str(intent.broker_price),
            str(intent.max_notional),
        ]
    )


def _extract_order_id(payload: Any) -> str | None:
    if isinstance(payload, list) and payload:
        value = payload[0].get("order_id")
        return str(value) if value not in {None, ""} else None
    if isinstance(payload, dict):
        value = payload.get("order_id")
        return str(value) if value not in {None, ""} else None
    return None


def _read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            event["source_file"] = str(path)
            events.append(_sanitize(event))
    return events


def _truthy(value: Any) -> bool:
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise ExecutionValidationError(f"{key} 不能为空。")
    return str(value).strip()


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = _required_str(payload, key)
    parsed = int(float(value))
    if parsed <= 0:
        raise ExecutionValidationError(f"{key} 必须为正数。")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    parsed = int(float(value))
    if parsed <= 0:
        raise ExecutionValidationError("数量必须为正数。")
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return Decimal(str(value))


def _first_query_value(
    query: dict[str, list[str]],
    key: str,
    default: str = "",
) -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _sanitize(value: Any) -> Any:
    sensitive_keys = {"password", "trade_password", "futu_trade_password"}
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in sensitive_keys:
                sanitized[key] = "***"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _dataclass_to_payload(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return {key: _dataclass_to_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _dataclass_to_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dataclass_to_payload(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _dataclass_to_payload(value)
    return str(value)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
