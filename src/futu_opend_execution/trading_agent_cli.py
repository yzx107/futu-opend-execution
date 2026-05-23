"""Compatibility entry point for ``python -m futu_opend_execution.trading_agent_cli``."""

from futu_opend_execution.cli.main import build_parser, main

__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
