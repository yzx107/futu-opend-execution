"""Compatibility entry point for the local OpenD Trading Agent Web console."""

from futu_opend_execution.web.app import create_app, main

__all__ = ["create_app", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
