from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from enterprise_mind.api.diagnostics import (
    api_category,
    capture_unexpected_api_error,
    redact_request_id,
    stack_location,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/admin/database-mcp/schema?secret=must-not-log", "admin.database_mcp"),
        ("/api/v1/admin/users/123", "admin.users"),
        ("/api/v1/admin/departments/456", "admin.departments"),
        ("/api/v1/conversations/789/messages", "api"),
        ("/health/ready", "health"),
        ("/internal", "other"),
    ],
)
def test_api_category_drops_query_and_resource_ids(path: str, expected: str):
    assert api_category(path) == expected


def test_request_id_and_stack_location_are_bounded():
    assert redact_request_id("0123456789abcdef") == "0123456789ab"

    try:
        raise RuntimeError("private SQL, parameter and response row")
    except RuntimeError as error:
        location = stack_location(error)
    assert location.endswith(":test_request_id_and_stack_location_are_bounded")
    assert "private SQL" not in location


@pytest.mark.parametrize(
    "filename",
    [
        r"C:\private\enterprise\handler.py",
        "/home/private/enterprise/handler.py",
    ],
)
def test_stack_location_hides_foreign_platform_directories(filename: str):
    def failure():
        raise RuntimeError("private SQL and credentials")

    failure.__code__ = failure.__code__.replace(co_filename=filename)
    try:
        failure()
    except RuntimeError as error:
        location = stack_location(error)
    assert location.startswith("handler.py:")
    assert "private" not in location and "enterprise" not in location


def test_capture_logs_only_redacted_request_diagnostics(caplog: pytest.LogCaptureFixture):
    app = FastAPI()
    app.state.database_client = object()
    app.state.queue = None
    app.state.storage = object()
    app.state.phase9_generation_jobs = {1: object()}
    app.state.agent = type("Agent", (), {"tasks": {2: object()}})()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/admin/database-mcp/schema/123",
            "query_string": b"password=do-not-log",
            "headers": [],
            "app": app,
        }
    )
    request.state.request_id = "0123456789abcdef"

    with caplog.at_level(logging.ERROR, logger="enterprise_mind.api"):
        try:
            raise RuntimeError("PRIVATE SQL password=do-not-log rows=must-not-log")
        except RuntimeError as error:
            capture_unexpected_api_error(request, error)

    message = caplog.records[-1].getMessage()
    assert "request_id=0123456789ab" in message
    assert "0123456789abcdef" not in message
    assert "api_category=admin.database_mcp" in message
    assert "exception_type=RuntimeError" in message
    assert "stack_location=test_api_diagnostics.py:" in message
    assert "database_client=configured" in message
    assert "queue=absent" in message
    assert "generation_jobs=1" in message
    assert "agent_tasks=1" in message
    assert "PRIVATE SQL" not in message
    assert "password=do-not-log" not in message
    assert "rows=must-not-log" not in message
    assert "Traceback" not in caplog.text
