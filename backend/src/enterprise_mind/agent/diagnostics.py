"""Bounded failure metadata; never log model inputs, outputs, or exception text."""

import logging

from pydantic import ValidationError

from enterprise_mind.api.diagnostics import stack_location
from enterprise_mind.api.errors import ErrorCode

LOGGER = logging.getLogger("enterprise_mind.agent")


def record_failure(stage: str, error: Exception, *, run_id: int | None = None) -> None:
    status = getattr(error, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    validation = (
        sorted({item["type"] for item in error.errors(include_input=False, include_context=False)})[
            :8
        ]
        if isinstance(error, ValidationError)
        else []
    )
    # Only known public error codes are safe; never pass arbitrary error text.
    code = error.args[0] if error.args and isinstance(error.args[0], str) else None
    safe_code = code if code in ErrorCode else None
    LOGGER.warning(
        "agent_failure stage=%s run_id=%s exception_type=%s http_status=%s "
        "code=%s validation_types=%s stack_location=%s",
        stage,
        run_id,
        type(error).__name__,
        status,
        safe_code,
        validation,
        stack_location(error),
    )
