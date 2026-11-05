"""Shared script invocation identity; never substitutes for an event time."""

from datetime import datetime, timedelta, timezone
import re
import time


class RunIdentityError(ValueError):
    """Invalid scheme / run id."""


def validate_run_id(value: str) -> str:
    """1-96 ASCII letters/digits/._-, starting with a letter or digit; no Windows reserved device names."""
    if not isinstance(value, str) or value.endswith('.') or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise RunIdentityError("run id must contain 1-96 ASCII letters/digits/._- and start with a letter or digit")
    if value.split('.')[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise RunIdentityError("run id cannot use a Windows reserved device name")
    return value


def resolve_script_id(value: str | None = None) -> str:
    if value is not None:
        return validate_run_id(value)
    stamp = time.time_ns()
    date = datetime.fromtimestamp(stamp // 1_000_000_000, timezone(timedelta(hours=8)))
    return f"{date:%Y%m%d}-{stamp}"
