"""Shared script invocation identity; never substitutes for an event time."""

from datetime import datetime, timedelta, timezone
import time

from .workflow_config import validate_run_id


def resolve_script_id(value: str | None = None) -> str:
    if value is not None:
        return validate_run_id(value)
    stamp = time.time_ns()
    date = datetime.fromtimestamp(stamp // 1_000_000_000, timezone(timedelta(hours=8)))
    return f"{date:%Y%m%d}-{stamp}"
