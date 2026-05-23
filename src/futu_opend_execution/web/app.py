"""Small local Web console for the OpenD Trading Agent."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse


DEFAULT_LOG = Path("logs/agent/monitor.jsonl")
DEFAULT_KILL_SWITCH = Path("logs/agent/KILL_SWITCH")


class WebState:
    def __init__(self, *, log_path: Path, kill_switch_file: Path) -> None:
        self.log_path = log_path
        self.kill_switch_file = kill_switch_file
        self.last_error: str | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "service": "opend-trading-agent",
            "mode": "dry-run-default",
            "kill_switch_active": self.kill_switch_file.exists(),
            "log_path": str(self.log_path),
            "last_error": self.last_error,
            "latest_signal": _tail_jsonl(self.log_path, limit=1),
        }


def create_app(*, log_path: Path = DEFAULT_LOG, kill_switch_file: Path = DEFAULT_KILL_SWITCH):
    state = WebState(log_path=log_path, kill_switch_file=kill_switch_file)

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
                state.kill_switch_file.unlink(missing_ok=True)
                return self._send_json(state.snapshot())
            self.send_error(404)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _send_json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        create_app(log_path=Path(args.log_path), kill_switch_file=Path(args.kill_switch_file)),
    )
    print(f"OpenD Trading Agent Web: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


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
    <button onclick="post('/api/kill-switch/clear')">清除 kill switch</button>
  </section>
  <section><strong>日志 tail</strong><pre id="logs"></pre></section>
</main>
<script>
async function load() {
  document.getElementById('state').textContent = JSON.stringify(await (await fetch('/api/state')).json(), null, 2);
  document.getElementById('logs').textContent = JSON.stringify(await (await fetch('/api/logs/tail')).json(), null, 2);
}
async function post(path) { await fetch(path, {method: 'POST'}); await load(); }
load(); setInterval(load, 3000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
