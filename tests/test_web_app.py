from __future__ import annotations

import unittest
import http.client
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from futu_opend_execution.web.app import KILL_SWITCH_CLEAR_PHRASE, _index_html, create_app, validate_web_host


class WebAppTests(unittest.TestCase):
    def test_main_page_contains_agent_sections(self) -> None:
        page = _index_html()
        self.assertIn("OpenD Trading Agent", page)
        self.assertIn("Kill Switch", page)
        self.assertIn("/api/state", page)

    def test_non_loopback_host_rejected_by_default(self) -> None:
        with self.assertRaises(SystemExit):
            validate_web_host("0.0.0.0")

    def test_health_state_and_kill_switch_clear_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kill = Path(temp_dir) / "KILL"
            handler = create_app(log_path=Path(temp_dir) / "monitor.jsonl", kill_switch_file=kill)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            conn = http.client.HTTPConnection(host, port)
            try:
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()

                conn.request("GET", "/api/state")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()

                conn.request("POST", "/api/kill-switch/create", body="{}", headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                self.assertTrue(kill.exists())

                conn.request("POST", "/api/kill-switch/clear", body="{}", headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                self.assertTrue(kill.exists())

                body = f'{{"confirm":"{KILL_SWITCH_CLEAR_PHRASE}"}}'
                conn.request("POST", "/api/kill-switch/clear", body=body, headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                self.assertFalse(kill.exists())
            finally:
                conn.close()
                server.shutdown()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
