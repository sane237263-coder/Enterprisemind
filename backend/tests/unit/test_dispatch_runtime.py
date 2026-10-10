"""Dispatcher retry boundaries; real PostgreSQL outage is a separate probe."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from enterprise_mind.infrastructure.settings import ApplicationConfig, DatabaseSettings
from enterprise_mind.ingestion import runtime


def database_error(sqlstate, *, invalidated=False):
    original = RuntimeError("synthetic driver failure")
    original.sqlstate = sqlstate
    return DBAPIError(None, None, original, connection_invalidated=invalidated)


@pytest.fixture
def dispatch_runtime(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    queue = SimpleNamespace(close=AsyncMock())
    storage = SimpleNamespace(check=lambda: None)
    dispatcher = SimpleNamespace(dispatch_once=AsyncMock(return_value=0))
    ready = AsyncMock()
    sweep = AsyncMock()
    sleep = AsyncMock()
    connection = SimpleNamespace(scalar=AsyncMock(return_value=1), commit=AsyncMock())

    @asynccontextmanager
    async def scheduler_slot(_engine):
        yield connection

    monkeypatch.setattr(runtime, "_scheduler_slot", scheduler_slot)
    monkeypatch.setattr(runtime, "IngestionSettings", lambda: None)
    monkeypatch.setattr(runtime, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(runtime, "session_factory", lambda *args: None)
    monkeypatch.setattr(runtime, "TaskQueue", lambda *args: queue)
    monkeypatch.setattr(runtime, "ObjectStorage", lambda *args: storage)
    monkeypatch.setattr(runtime, "Dispatcher", lambda *args: dispatcher)
    monkeypatch.setattr(runtime, "check_database", ready)
    monkeypatch.setattr(runtime, "sweep_orphans", sweep)
    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    settings = DatabaseSettings.model_construct(env="test", postgres_user="test")
    return SimpleNamespace(
        settings=settings,
        config=ApplicationConfig(),
        dispatcher=dispatcher,
        ready=ready,
        sweep=sweep,
        sleep=sleep,
        engine=engine,
        queue=queue,
    )


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionResetError(),
        PoolTimeoutError(),
        database_error("08006"),
        database_error("57P01"),
        database_error("57P02"),
        database_error("57P03"),
        database_error(None, invalidated=True),
    ],
)
async def test_transient_outage_recovers_with_original_polling_and_cancellation(
    dispatch_runtime, failure
):
    case = dispatch_runtime
    case.dispatcher.dispatch_once.side_effect = [failure, 0, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await runtime.run_processing("dispatch", case.settings, case.config, once=False)
    assert case.dispatcher.dispatch_once.await_count == 3
    assert case.sweep.await_count == 1
    assert case.sleep.await_count == 2
    assert all(
        call.args == (case.config.outbox.poll_interval_seconds,)
        for call in case.sleep.await_args_list
    )
    case.queue.close.assert_awaited_once()
    case.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionRefusedError(),
        database_error("57P01"),
    ],
)
async def test_once_preserves_failure_and_releases_resources(dispatch_runtime, failure):
    case = dispatch_runtime
    case.dispatcher.dispatch_once.side_effect = failure
    with pytest.raises(type(failure)):
        await runtime.run_processing("dispatch", case.settings, case.config, once=True)
    assert case.dispatcher.dispatch_once.await_count == 1
    case.sleep.assert_not_awaited()
    case.queue.close.assert_awaited_once()
    case.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [
        database_error("23505"),
        database_error("42501"),
        database_error("42601"),
        database_error("57014"),
        ValueError("invalid dispatch state"),
        FileNotFoundError(),
    ],
)
async def test_nonconnection_errors_are_not_hidden_in_retry_loop(dispatch_runtime, failure):
    case = dispatch_runtime
    case.dispatcher.dispatch_once.side_effect = failure
    with pytest.raises(type(failure)):
        await runtime.run_processing("dispatch", case.settings, case.config, once=False)
    assert case.dispatcher.dispatch_once.await_count == 1
    case.sleep.assert_not_awaited()
    case.queue.close.assert_awaited_once()
    case.engine.dispose.assert_awaited_once()


async def test_startup_readiness_still_fails_once(dispatch_runtime):
    case = dispatch_runtime
    case.ready.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await runtime.run_processing("dispatch", case.settings, case.config, once=False)
    case.dispatcher.dispatch_once.assert_not_awaited()
    case.sleep.assert_not_awaited()
    case.engine.dispose.assert_awaited_once()
