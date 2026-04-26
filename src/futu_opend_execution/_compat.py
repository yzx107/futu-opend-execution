"""Compatibility helpers for Python runtime differences."""

from __future__ import annotations

import datetime as _datetime
import enum as _enum

if hasattr(_enum, "StrEnum"):
    StrEnum = _enum.StrEnum
else:

    class StrEnum(str, _enum.Enum):
        """Compatibility shim for Python <3.11."""


UTC = getattr(_datetime, "UTC", _datetime.timezone.utc)
