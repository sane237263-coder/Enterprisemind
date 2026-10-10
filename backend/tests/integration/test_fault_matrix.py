# ruff: noqa: F811

"""Fault contracts using isolated PostgreSQL, Redis and MinIO.

Filesystem errors are injected only at the attempt's temporary output path.
Confirmation recovery uses the real polling loop and persisted checkpoint.
"""

import asyncio
import errno
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent import agent, new_run, snapshot  # noqa: F401
from test_agent_lifecycle import WriteModel, pending, respond, setup_folder
from test_database_admin import _refresh, database_app  # noqa: F401
from test_generation_failure_contracts import (
    _create_render_task,
    _deliver_generation_event,
    _task_snapshot,
    _track_attempt_keys,
)
from test_ingestion import ingestion_runtime  # noqa: F401
from test_phase8_acceptance import (  # noqa: F401
    binding,
    catalog,
    configured,
    policy,
    verify_binding,
)

from enterprise_mind.agent.generation_models import GenerationTask
from enterprise_mind.api.errors import APIError
from enterprise_mind.database.contracts import SchemaCatalog
from enterprise_mind.database.models import RelationDepartmentGrant, RelationPolicy
from enterprise_mind.ingestion.models import Folder, OutboxEvent
from enterprise_mind.knowledge.models import DeletionBatch
from enterprise_mind.knowledge.pending_models import PendingAction

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", ["permission", "disk_full"])
def test_output_write_failure_releases_attempt_without_file(
    runtime, database, ingestion_settings, monkeypatch, failure
):
    task_id, event_id = _create_render_task(runtime)
    keys = _track_attempt_keys(runtime.worker.generation.storage, monkeypatch)
    original = Path.open
    directories = []

    def fail_attempt_write(path, mode="r", *args, **kwargs):
        if path.parent.name.startswith("em-generation-") and "w" in mode:
            directories.append(path.parent)
            if failure == "permission":
                raise PermissionError(errno.EACCES, "Synthetic isolated permission failure")
            raise OSError(errno.ENOSPC, "Synthetic isolated disk full")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_attempt_write)
    assert _deliver_generation_event(runtime)
    state = _task_snapshot(database, task_id, keys)
    assert state["status"] == "failed" and state["error_code"] == "GENERATION_FAILED"
    assert state["file_id"] is None and state["generated_file_ids"] == []
    assert directories and all(not path.exists() for path in directories)
    assert keys == []
    with Session(database.engine) as session:
        task = session.get(GenerationTask, task_id)
        assert task.execution_lease_expires_at is None
        assert task.finished_at is not None
        outbox = session.get(OutboxEvent, event_id)
        assert outbox.status == "published" and outbox.lease_owner is None
    _, root_storage, queue = ingestion_settings
    assert list(root_storage.list_objects(runtime.worker.generation.storage.bucket)) == []
    assert queue.xpending(runtime.worker.queue.stream, "ingestion-workers")["pending"] == 0


def test_accepted_confirmation_recovers_without_wakeup_or_second_response(agent, database):
    client, _, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    assert client.portal.call(manager.execute, run_id)
    identifier, _ = pending(database, run_id)
    assert respond(agent, identifier).status_code == 200
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).deleted_at is None
        assert session.get(PendingAction, identifier).status != "pending"

    async def recover():
        loop = asyncio.create_task(manager.loop())
        try:
            async with asyncio.timeout(15):
                while True:
                    async with manager.sessions() as session:
                        from enterprise_mind.agent.models import AgentRun

                        run = await session.get(AgentRun, run_id)
                        if run.status in {"completed", "failed"}:
                            assert run.status == "completed"
                            return
                    await asyncio.sleep(0.05)
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)

    client.portal.call(recover)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert not client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(DeletionBatch)) == 1
        assert session.get(Folder, int(folder)).row_version == 2
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 1


def test_schema_refresh_racing_policy_update_cannot_transfer_old_object_grant(configured, database):
    client, app, admin, _, _ = configured
    granted = policy(client, admin, ("biz", "orders"), [])
    assert granted.status_code == 200
    old = client.portal.call(binding, app, "初始管理员")
    request = client.post(
        "/api/v1/admin/database-mcp/schema-refresh",
        headers={**admin, "Idempotency-Key": "matrix-racing-refresh"},
    )
    assert request.status_code == 202
    snapshot_id = int(request.json()["data"]["id"])
    reached, release = threading.Event(), threading.Event()
    changed = SchemaCatalog(
        tuple(
            replace(item, relation_oid=999) if item.key == ("biz", "orders") else item
            for item in catalog().relations
        )
    )

    async def fetch(_source):
        reached.set()
        assert await asyncio.to_thread(release.wait, 8)
        return changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(client.portal.call, _refresh, app, snapshot_id, fetch)
        try:
            assert reached.wait(8)
            update = pool.submit(policy, client, admin, ("biz", "orders"), [], version=1)
            release.set()
            assert refresh.result(timeout=10).status == "active"
            assert update.result(timeout=10).status_code in {200, 409}
        finally:
            release.set()
    assert client.portal.call(binding, app, "初始管理员").relation_keys == set()
    with pytest.raises(APIError):
        client.portal.call(verify_binding, app, "初始管理员", old)
    with Session(database.engine) as session:
        row = session.get(RelationPolicy, int(granted.json()["data"]["id"]))
        assert row.enabled is False and row.row_version >= 2
        assert not list(
            session.scalars(
                select(RelationDepartmentGrant).where(
                    RelationDepartmentGrant.relation_policy_id == row.id
                )
            )
        )
