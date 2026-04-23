"""Shared runtime helpers for importing the optional futu SDK safely."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution.broker import BrokerDependencyError


def load_futu_module(config: RuntimeConfig):
    """Load the optional futu SDK after preparing a writable HOME if needed."""

    if config.futu_sdk_home_override:
        home_path = Path(config.futu_sdk_home_override).expanduser()
        log_path = home_path / ".com.futunn.FutuOpenD" / "Log"
        log_path.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(home_path)

    try:
        return importlib.import_module("futu")
    except ImportError as exc:
        raise BrokerDependencyError(
            "futu-api is not installed in the active Python environment. "
            "Install it with `pip install futu-api` or run this package with "
            "the interpreter that already has futu-api available."
        ) from exc
