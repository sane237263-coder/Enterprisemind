"""Closed cursor/transaction failures differ from live connection misuse."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.mcp_server import _export_transaction


@pytest.mark.parametrize("stage", ["enter", "body", "exit"])
@pytest.mark.parametrize("connection_state", ["live", "closed", "detached"])
async def test_interface_failure_classification_before_pool_release(stage, connection_state):
    failure = asyncpg.InterfaceError("synthetic private connection details")
    options = {}

    @asynccontextmanager
    async def transaction(**kwargs):
        options.update(kwargs)
        if stage == "enter":
            raise failure
        try:
            yield
        finally:
            if stage == "exit":
                raise failure

    def is_closed():
        if connection_state == "detached":
            raise asyncpg.InterfaceError("synthetic released connection proxy")
        return connection_state == "closed"

    connection = SimpleNamespace(transaction=transaction, is_closed=is_closed)
    expected = asyncpg.InterfaceError if connection_state == "live" else APIError
    with pytest.raises(expected) as captured:
        async with _export_transaction(connection):
            if stage == "body":
                raise failure
    assert options == {"isolation": "repeatable_read", "readonly": True}
    if connection_state != "live":
        assert captured.value.code == ErrorCode.DATABASE_SOURCE_UNAVAILABLE
        assert captured.value.details == {} and captured.value.__suppress_context__
        assert "private" not in str(captured.value)
    else:
        assert captured.value is failure


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.CancelledError(),
        TimeoutError(),
        asyncpg.InsufficientPrivilegeError("synthetic permission failure"),
        APIError(ErrorCode.DATABASE_RELATION_FORBIDDEN),
    ],
)
async def test_noninterface_errors_are_preserved(failure):
    @asynccontextmanager
    async def transaction(**_kwargs):
        yield

    connection = SimpleNamespace(transaction=transaction, is_closed=lambda: True)
    with pytest.raises(type(failure)) as captured:
        async with _export_transaction(connection):
            raise failure
    assert captured.value is failure
