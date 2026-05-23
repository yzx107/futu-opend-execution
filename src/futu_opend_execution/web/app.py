"""Small local Web console for the OpenD Trading Agent."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from futu_opend_execution.config import is_local_opend_host
from futu_opend_execution.watchlist import WatchlistConfigError, load_watchlist_config


DEFAULT_LOG = Path("logs/agent/monitor.jsonl")
DEFAULT_KILL_SWITCH = Path("logs/agent/KILL_SWITCH")
KILL_SWITCH_CLEAR_PHRASE = "CLEAR_KILL_SWITCH"


class WebState:
    def __init__(
        self,
        *,
        log_path: Path,
        kill_switch_file: Path,
        watchlist_config: Path | None = None,
        paper_report: Path | None = None,
        clear_token: str | None = None,
    ) -> None:
        self.log_path = log_path
        self.kill_switch_file = kill_switch_file
        self.watchlist_config = watchlist_config
        self.paper_report = paper_report
        self.clear_token = clear_token
        self.last_error: str | None = None

    def snapshot(self) -> dict[str, object]:
        rows = _tail_jsonl(self.log_path, limit=200)
        return {
            "service": "opend-trading-agent",
            "service_status": "kill-switch-active" if self.kill_switch_file.exists() else "ready",
            "mode": "dry-run-default",
            "kill_switch_active": self.kill_switch_file.exists(),
            "log_path": str(self.log_path),
            "last_error": self.last_error,
            "watchlist": _read_watchlist(self.watchlist_config),
            "latest_market_state": _latest_event(rows, "market_state"),
            "latest_signal": _latest_event(rows, "strategy_signal"),
            "latest_risk_event": _latest_event(rows, "risk_event"),
            "paper_summary": _read_json(self.paper_report),
        }


def create_app(
    *,
    log_path: Path = DEFAULT_LOG,
    kill_switch_file: Path = DEFAULT_KILL_SWITCH,
    watchlist_config: Path | None = None,
    paper_report: Path | None = None,
    clear_token: str | None = None,
):
    state = WebState(
        log_path=log_path,
        kill_switch_file=kill_switch_file,
        watchlist_config=watchlist_config,
        paper_report=paper_report,
        clear_token=clear_token,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                return self._send_html(_index_html())
            if path == "/api/health":
                return self._send_json({"ok": True, "local_only": True})
            if path == "/api/state":
                return self._send_json(state.snapshot())
            if path == "/api/logs/tail":
                return self._send_json({"rows": _tail_jsonl(state.log_path, limit=50)})
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/kill-switch/create":
                state.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
                state.kill_switch_file.write_text("blocked\n", encoding="utf-8")
                return self._send_json(state.snapshot())
            if path == "/api/kill-switch/clear":
                body = self._read_json_body()
                phrase_ok = body.get("confirm") == KILL_SWITCH_CLEAR_PHRASE
                token_ok = bool(state.clear_token) and body.get("token") == state.clear_token
                if not phrase_ok and not token_ok:
                    return self._send_json({"ok": False, "error": "kill switch clear requires confirmation phrase or token"}, status=403)
                state.kill_switch_file.unlink(missing_ok=True)
                return self._send_json(state.snapshot())
            self.send_error(404)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _send_json(self, payload: dict[str, object], *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="ignore")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenD Trading Agent local Web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-path", default=str(DEFAULT_LOG))
    parser.add_argument("--kill-switch-file", default=str(DEFAULT_KILL_SWITCH))
    parser.add_argument("--watchlist-config", default=None)
    parser.add_argument("--paper-report", default=None)
    parser.add_argument("--clear-token", default=None)
    parser.add_argument("--allow-non-loopback-dev", action="store_true")
    args = parser.parse_args(argv)
    validate_web_host(args.host, allow_non_loopback_dev=args.allow_non_loopback_dev)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        create_app(
            log_path=Path(args.log_path),
            kill_switch_file=Path(args.kill_switch_file),
            watchlist_config=Path(args.watchlist_config) if args.watchlist_config else None,
            paper_report=Path(args.paper_report) if args.paper_report else None,
            clear_token=args.clear_token,
        ),
    )
    print(f"OpenD Trading Agent Web: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


def validate_web_host(host: str, *, allow_non_loopback_dev: bool = False) -> None:
    if is_local_opend_host(host):
        return
    if allow_non_loopback_dev:
        return
    raise SystemExit("web console host must be loopback unless --allow-non-loopback-dev is set")


def _tail_jsonl(path: Path, *, limit: int) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return rows


def _latest_event(rows: list[dict[str, object]], event: str) -> dict[str, object] | None:
    for row in reversed(rows):
        if row.get("event") == event:
            return row
    return None


def _read_json(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _read_watchlist(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        return load_watchlist_config(path).to_jsonable()
    except (WatchlistConfigError, OSError):
        return {"error": "watchlist unavailable"}


def _index_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>OpenD Trading Agent</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 32px; color: #111827; background: #f8fafc; }
    main { max-width: 960px; margin: auto; }
    section { background: white; border: 1px solid #d1d5db; border-radius: 8px; padding: 20px; margin: 16px 0; }
    button { padding: 8px 12px; border: 1px solid #9ca3af; border-radius: 6px; background: #fff; cursor: pointer; }
    button.danger { color: #991b1b; border-color: #fca5a5; }
    pre { white-space: pre-wrap; background: #111827; color: #f9fafb; padding: 12px; border-radius: 6px; min-height: 80px; }
  </style>
</head>
<body>
<main>
  <h1>OpenD Trading Agent</h1>
  <section><strong>状态</strong><pre id="state">loading...</pre></section>
  <section>
    <strong>Kill Switch</strong><br><br>
    <button class="danger" onclick="post('/api/kill-switch/create')">创建 kill switch</button>
    <button onclick="clearKillSwitch()">清除 kill switch</button>
  </section>
  <section><strong>日志 tail</strong><pre id="logs"></pre></section>
</main>
<script>
async function load() {
  document.getElementById('state').textContent = JSON.stringify(await (await fetch('/api/state')).json(), null, 2);
  document.getElementById('logs').textContent = JSON.stringify(await (await fetch('/api/logs/tail')).json(), null, 2);
}
async function post(path, body) { await fetch(path, {method: 'POST', body: JSON.stringify(body || {}), headers: {'Content-Type': 'application/json'}}); await load(); }
async function clearKillSwitch() {
  const confirm = window.prompt('confirm');
  await post('/api/kill-switch/clear', {confirm});
}
load(); setInterval(load, 3000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
