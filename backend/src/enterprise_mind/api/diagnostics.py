"""Redacted request diagnostics for locating unexpected API failures.

The diagnostic record is deliberately smaller than an exception traceback.  It
keeps the request correlation prefix and the final source location while
excluding URL parameters, exception text, SQL, bind values, and response rows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from traceback import extract_tb
from typing import Any

from fastapi import Request

LOGGER = logging.getLogger("enterprise_mind.api")


def api_category(path: str) -> str:
    """Map an API path to a stable category without retaining resource IDs."""

    path = path.split("?", 1)[0]
    if path.startswith("/api/v1/admin/database-mcp/"):
        return "admin.database_mcp"
    if path.startswith("/api/v1/admin/users"):
        return "admin.users"
    if path.startswith("/api/v1/admin/departments"):
        return "admin.departments"
    if path.startswith("/api/v1/admin/"):
        return "admin"
    if path.startswith("/api/v1/"):
        return "api"
    if path.startswith("/health/"):
        return "health"
    return "other"


def redact_request_id(value: object) -> str:
    """Return enough of a generated request ID for correlation, never all of it."""

    text = str(value)
    return text[:12] if len(text) > 12 else text


def stack_location(error: BaseException) -> str:
    """Return only the last source filename, line, and function."""

    frames = extract_tb(error.__traceback__)
    if not frames:
        return f"<{type(error).__name__}:no-frame>"
    frame = frames[-1]
    # Tracebacks can retain a Windows filename in cached or transported code on Linux.
    filename = Path(frame.filename.replace("\\", "/")).name
    return f"{filename}:{frame.lineno}:{frame.name}"


def runtime_state(request: Request) -> str:
    """Summarize only configured connection/task state for an API failure."""

    state: Any = request.app.state
    values = {
        "database_client": "configured" if getattr(state, "database_client", None) else "absent",
        "queue": "configured" if getattr(state, "queue", None) else "absent",
        "storage": "configured" if getattr(state, "storage", None) else "absent",
    }
    jobs = getattr(state, "phase9_generation_jobs", None)
    values["generation_jobs"] = str(len(jobs)) if isinstance(jobs, dict) else "unknown"
    agent = getattr(state, "agent", None)
    tasks = getattr(agent, "tasks", None)
    values["agent_tasks"] = str(len(tasks)) if isinstance(tasks, dict) else "unknown"
    return ",".join(f"{key}={values[key]}" for key in sorted(values))


def capture_unexpected_api_error(request: Request, error: BaseException) -> None:
    """Emit a safe, single-line diagnostic for an unexpected request failure.

    Do not use ``logger.exception`` here: its traceback includes exception text,
    which can accidentally contain SQL, parameters, provider responses, or
    other request data.  The explicit final frame is enough to locate the
    failure while the API response remains the user-facing error contract.
    """

    request_id = getattr(getattr(request, "state", None), "request_id", "unknown")
    LOGGER.error(
        "api_request_failed request_id=%s api_category=%s method=%s "
        "exception_type=%s stack_location=%s runtime=%s",
        redact_request_id(request_id),
        api_category(request.url.path),
        request.method,
        type(error).__name__,
        stack_location(error),
        runtime_state(request),
    )


__all__ = [
    "LOGGER",
    "api_category",
    "capture_unexpected_api_error",
    "redact_request_id",
    "runtime_state",
    "stack_location",
]
