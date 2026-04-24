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
from datetime import UTC, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import BrokerError
from futu_opend_execution.grey_open import (
    FutuGreyMarketOpenDClient,
    GreyMarketOpenTrigger,
    GreyMarketTriggerRules,
    JsonlEventLogger,
    log_signal,
)
from futu_opend_execution.normal_trade import (
    FutuNormalTradeClient,
    NormalOrderType,
    NormalQuantityMode,
    NormalTradeSide,
    build_normal_trade_intent,
    load_dotenv,
)
from futu_opend_execution.risk import ExecutionValidationError, validate_runtime_config


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
                self._handle_json(lambda: api_health(state))
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


def api_health(state: WebState) -> dict[str, Any]:
    validate_runtime_config(state.config)
    return {
        "status": "READY",
        "host": state.config.futu_host,
        "port": state.config.futu_port,
        "allow_real_trade": state.config.allow_real_trade,
        "kill_switch": state.kill_switch_file.exists(),
        "kill_switch_file": str(state.kill_switch_file),
        "log_file": str(state.log_file),
    }


def api_quote(state: WebState, *, symbol: str) -> dict[str, Any]:
    if not symbol:
        raise ExecutionValidationError("请输入标的代码。")
    with FutuNormalTradeClient(state.config) as client:
        quote = client.read_quote(symbol)
    payload = _quote_to_payload(quote)
    _log_event(state, "web_quote", quote=payload)
    return {"quote": payload}


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
