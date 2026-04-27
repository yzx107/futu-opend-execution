from __future__ import annotations

import unittest
from unittest.mock import patch

from futu_opend_execution.config import (
    RuntimeConfig,
    harden_local_opend_environment,
    is_local_opend_host,
)
from futu_opend_execution.execution.futu_runtime import load_futu_module
from futu_opend_execution.risk import ExecutionValidationError, validate_runtime_config


class RuntimeSafetyTests(unittest.TestCase):
    def test_runtime_config_requires_local_opend_host(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            with self.subTest(host=host):
                validate_runtime_config(RuntimeConfig(futu_host=host))

        with self.assertRaises(ExecutionValidationError):
            validate_runtime_config(RuntimeConfig(futu_host="192.168.1.10"))

    def test_local_opend_host_detection_rejects_non_loopback_names(self) -> None:
        self.assertTrue(is_local_opend_host("127.0.0.1"))
        self.assertTrue(is_local_opend_host("localhost"))
        self.assertFalse(is_local_opend_host("opend.example.com"))

    def test_harden_local_opend_environment_clears_proxy_env(self) -> None:
        env = {
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "NO_PROXY": "example.com",
        }

        removed = harden_local_opend_environment(env)

        self.assertEqual(removed["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(removed["https_proxy"], "http://127.0.0.1:7890")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("https_proxy", env)
        self.assertIn("127.0.0.1", env["NO_PROXY"])
        self.assertIn("localhost", env["NO_PROXY"])
        self.assertIn("::1", env["NO_PROXY"])

    def test_load_futu_module_hardens_environment_before_import(self) -> None:
        fake_module = object()
        env = {"ALL_PROXY": "socks5://127.0.0.1:7890"}

        with patch(
            "futu_opend_execution.config.environ",
            env,
        ), patch(
            "futu_opend_execution.execution.futu_runtime.importlib.import_module",
            return_value=fake_module,
        ):
            loaded = load_futu_module(RuntimeConfig())

        self.assertIs(loaded, fake_module)
        self.assertNotIn("ALL_PROXY", env)
        self.assertIn("127.0.0.1", env["NO_PROXY"])


if __name__ == "__main__":
    unittest.main()
