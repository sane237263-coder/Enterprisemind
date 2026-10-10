# ruff: noqa: F811

"""Failure and cancellation contracts for the real Phase 10 render chain.

These tests reuse the isolated PostgreSQL, Redis, and MinIO fixtures.  They
create frozen drafts directly and do not call a model, MCP server, or browser.
Fault injection is limited to renderer/storage/DB boundaries and thread events;
all database and object-storage operations remain real.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.api.errors import ErrorCode
from enterprise_mind.generation import service as generation_service
from enterprise_mind.generation import worker as generation_worker_module
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import (
    Document,
    OutboxEvent,
    Revision,
    StorageObject,
)
from enterprise_mind.knowledge.models import KnowledgeBase

pytestmark = pytest.mark.integration


def _create_render_task(
    runtime: Any,
    *,
    username: str = "初始管理员",
    source_refs: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    task_key = "phase10-negative-" + uuid.uuid4().hex

    async def create() -> tuple[int, int]:
        async with runtime.worker.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(User, Department)
                    .outerjoin(Department, Department.id == User.department_id)
                    .where(User.username == username)
                )
            ).one_or_none()
            assert row is not None
            user, department = row
            identity = CurrentIdentity(user, department, user.credential_version)
            draft = await generation_service.create_draft(
                session,
                identity,
                title="Phase 10 negative contract",
                body_markdown="# Frozen content\n\nThis is a real renderer input.",
                tables=[],
                source_refs=source_refs or [],
            )
            task = await generation_service.create_render_task(
                session,
                identity,
                draft_id=int(draft.id),
                expected_revision=int(draft.revision),
                file_format="md",
                file_name=task_key + ".md",
                idempotency_key=task_key,
            )
            outbox = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_type == "generation_task",
                    OutboxEvent.aggregate_id == task.id,
                    OutboxEvent.event_type == "generation.requested",
                )
            )
            assert outbox is not None
            return int(task.id), int(outbox.id)

    return runtime.client.portal.call(create)


def _track_attempt_keys(storage: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    original = storage.new_key
    keys: list[str] = []

    def tracked(prefix: str = "original", **kwargs: Any) -> str:
        key = original(prefix, **kwargs)
        keys.append(key)
        return key

    monkeypatch.setattr(storage, "new_key", tracked)
    return keys


def _deliver_generation_event(runtime: Any) -> bool:
    assert runtime.run(runtime.dispatcher.dispatch_once) == 1
    return bool(runtime.run(runtime.worker.run_once))


def _assert_attempt_objects_absent(root_storage: Any, bucket: str, keys: list[str]) -> None:
    visible = {item.object_name for item in root_storage.list_objects(bucket, recursive=True)}
    assert not (set(keys) & visible)


def _task_snapshot(database: Any, task_id: int, keys: list[str]) -> dict[str, Any]:
    with Session(database.engine) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        files = list(session.scalars(select(GeneratedFile).where(GeneratedFile.task_id == task_id)))
        objects = (
            list(session.scalars(select(StorageObject).where(StorageObject.object_key.in_(keys))))
            if keys
            else []
        )
        return {
            "status": task.status,
            "error_code": task.error_code,
            "file_id": task.file_id,
            "generated_file_ids": [int(item.id) for item in files],
            "storage_object_ids": [int(item.storage_object_id) for item in files],
            "object_keys": [item.object_key for item in objects],
        }


def _create_department_source(runtime: Any) -> tuple[str, int, int, dict[str, Any]]:
    username = "phase10-department-owner-" + uuid.uuid4().hex[:12]

    async def create() -> tuple[str, int, int, dict[str, Any]]:
        async with runtime.worker.sessions() as session, session.begin():
            admin = await session.scalar(select(User).where(User.username == "初始管理员"))
            assert admin is not None
            owner_department = Department(
                name="生成来源部门 A " + uuid.uuid4().hex[:8],
                name_key="generation-source-a-" + uuid.uuid4().hex,
            )
            other_department = Department(
                name="生成来源部门 B " + uuid.uuid4().hex[:8],
                name_key="generation-source-b-" + uuid.uuid4().hex,
            )
            session.add_all([owner_department, other_department])
            await session.flush()
            owner = User(
                username=username,
                username_key=username,
                password_hash=admin.password_hash,
                role="user",
                department_id=owner_department.id,
            )
            session.add(owner)
            await session.flush()
            base = KnowledgeBase(
                name="生成撤权来源 " + uuid.uuid4().hex[:8],
                name_key="generation-source-" + uuid.uuid4().hex,
                type="department",
                department_id=owner_department.id,
            )
            session.add(base)
            await session.flush()
            source_object = StorageObject(
                bucket_name=runtime.worker.storage.bucket,
                object_key="original/phase10-source/" + uuid.uuid4().hex,
                object_type="original",
                size_bytes=1,
                content_type="text/plain",
                content_sha256="a" * 64,
                status="available",
            )
            session.add(source_object)
            await session.flush()
            document = Document(
                knowledge_base_id=base.id,
                name="department-source.txt",
                name_key="department-source.txt",
                created_by=owner.id,
                processing_status="success",
                latest_revision_no=1,
            )
            session.add(document)
            await session.flush()
            revision = Revision(
                document_id=document.id,
                revision_no=1,
                trigger_type="initial_upload",
                original_object_id=source_object.id,
                original_file_name="department-source.txt",
                file_extension="txt",
                mime_type="text/plain",
                size_bytes=1,
                content_sha256="a" * 64,
                processing_status="success",
                initiated_by=owner.id,
            )
            session.add(revision)
            await session.flush()
            document.active_revision_id = revision.id
            return (
                username,
                int(base.id),
                int(other_department.id),
                {
                    "source_type": "knowledge",
                    "document_id": str(document.id),
                    "revision_id": str(revision.id),
                    "title": document.name,
                },
            )

    return runtime.client.portal.call(create)


def _create_empty_source_owner(runtime: Any) -> str:
    username = "phase10-empty-source-owner-" + uuid.uuid4().hex[:12]

    async def create() -> str:
        async with runtime.worker.sessions() as session, session.begin():
            admin = await session.scalar(select(User).where(User.username == "初始管理员"))
            assert admin is not None
            department = Department(
                name="无来源生成部门 " + uuid.uuid4().hex[:8],
                name_key="generation-empty-source-" + uuid.uuid4().hex,
            )
            session.add(department)
            await session.flush()
            session.add(
                User(
                    username=username,
                    username_key=username,
                    password_hash=admin.password_hash,
                    role="user",
                    department_id=department.id,
                )
            )
            return username

    return runtime.client.portal.call(create)


def test_renderer_exception_fails_without_publishing_a_file(
    runtime, database, ingestion_settings, monkeypatch
):
    task_id, _ = _create_render_task(runtime)
    storage = runtime.worker.generation.storage
    keys = _track_attempt_keys(storage, monkeypatch)

    def fail_render(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("injected renderer failure")

    monkeypatch.setattr(generation_worker_module, "render_to_path", fail_render)
    assert _deliver_generation_event(runtime)

    snapshot = _task_snapshot(database, task_id, keys)
    assert snapshot["status"] == "failed"
    assert snapshot["error_code"] == "GENERATION_FAILED"
    assert snapshot["file_id"] is None and snapshot["generated_file_ids"] == []
    assert keys == []
    _, root_storage, _ = ingestion_settings
    _assert_attempt_objects_absent(root_storage, storage.bucket, keys)


@pytest.mark.parametrize("failure_point", ["verify", "database_publish"])
def test_uploaded_attempt_is_removed_after_verify_or_database_publish_failure(
    runtime, database, ingestion_settings, monkeypatch, failure_point: str
):
    task_id, _ = _create_render_task(runtime)
    storage = runtime.worker.generation.storage
    keys = _track_attempt_keys(storage, monkeypatch)
    real_verify = storage.verify
    verified_keys: list[str] = []
    failure_listener = None
    publish_engine = None
    failure_injections: list[str] = []

    if failure_point == "verify":

        def verify_then_fail(key: str, size: int, digest: str) -> None:
            real_verify(key, size, digest)
            verified_keys.append(key)
            raise OSError("injected post-upload verification failure")

        monkeypatch.setattr(storage, "verify", verify_then_fail)
    else:

        def record_real_verify(key: str, size: int, digest: str) -> None:
            real_verify(key, size, digest)
            verified_keys.append(key)

        monkeypatch.setattr(storage, "verify", record_real_verify)

        def fail_generated_file_insert(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().replace('"', "").split())
            if normalized.startswith("insert into agent.generated_files"):
                failure_injections.append("generated_files")
                raise RuntimeError("injected database publication failure")

        async_engine = runtime.worker.generation.sessions.kw["bind"]
        publish_engine = async_engine.sync_engine
        event.listen(publish_engine, "before_cursor_execute", fail_generated_file_insert)
        failure_listener = fail_generated_file_insert

    try:
        assert _deliver_generation_event(runtime)
    finally:
        if failure_listener is not None and publish_engine is not None:
            event.remove(publish_engine, "before_cursor_execute", failure_listener)

    assert len(keys) == 1 and verified_keys == keys
    if failure_point == "database_publish":
        assert failure_injections == ["generated_files"]
    snapshot = _task_snapshot(database, task_id, keys)
    assert snapshot["status"] == "failed"
    assert snapshot["error_code"] == "GENERATION_FAILED"
    assert snapshot["file_id"] is None and snapshot["generated_file_ids"] == []
    assert snapshot["object_keys"] == []
    _, root_storage, _ = ingestion_settings
    _assert_attempt_objects_absent(root_storage, storage.bucket, keys)


def test_http_cancel_after_real_minio_upload_removes_the_attempt(
    runtime, database, ingestion_settings, monkeypatch
):
    task_id, _ = _create_render_task(runtime)
    storage = runtime.worker.generation.storage
    keys = _track_attempt_keys(storage, monkeypatch)
    entered_verify = threading.Event()
    release_verify = threading.Event()
    real_verify = storage.verify

    def verify_then_wait(key: str, size: int, digest: str) -> None:
        real_verify(key, size, digest)
        entered_verify.set()
        if not release_verify.wait(timeout=30):
            raise TimeoutError("test did not release the verified MinIO upload")

    monkeypatch.setattr(storage, "verify", verify_then_wait)
    _, root_storage, _ = ingestion_settings
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_deliver_generation_event, runtime)
        try:
            assert entered_verify.wait(timeout=30)
            assert len(keys) == 1
            assert root_storage.stat_object(storage.bucket, keys[0]).size > 0
            cancelled = runtime.client.post(
                f"/api/v1/generation-tasks/{task_id}/cancel", headers=runtime.admin
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["data"]["status"] == "cancelled"
            assert cancelled.json()["data"]["file_id"] is None
        finally:
            release_verify.set()
        assert future.result(timeout=30) is True

    snapshot = _task_snapshot(database, task_id, keys)
    assert snapshot["status"] == "cancelled"
    assert snapshot["file_id"] is None and snapshot["generated_file_ids"] == []
    assert snapshot["object_keys"] == []
    _assert_attempt_objects_absent(root_storage, storage.bucket, keys)


def test_department_source_access_revoked_before_publish_blocks_file(
    runtime, database, ingestion_settings, monkeypatch
):
    # The source rows are a real PostgreSQL authorization fixture. This
    # exercises access revalidation only; it does not claim ingestion/index
    # evidence for the referenced document.
    username, base_id, other_department_id, source_ref = _create_department_source(runtime)
    task_id, _ = _create_render_task(runtime, username=username, source_refs=[source_ref])
    storage = runtime.worker.generation.storage
    keys = _track_attempt_keys(storage, monkeypatch)
    rendered = threading.Event()
    release_renderer = threading.Event()
    real_render = generation_worker_module.render_to_path

    def render_then_wait(*args: Any, **kwargs: Any) -> Any:
        result = real_render(*args, **kwargs)
        rendered.set()
        if not release_renderer.wait(timeout=30):
            raise TimeoutError("test did not release the completed render")
        return result

    monkeypatch.setattr(generation_worker_module, "render_to_path", render_then_wait)
    _, root_storage, _ = ingestion_settings
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_deliver_generation_event, runtime)
        try:
            assert rendered.wait(timeout=30)
            with Session(database.engine) as session, session.begin():
                base = session.get(KnowledgeBase, base_id)
                assert base is not None
                base.department_id = other_department_id
            release_renderer.set()
        finally:
            release_renderer.set()
        assert future.result(timeout=30) is True

    snapshot = _task_snapshot(database, task_id, keys)
    assert snapshot["status"] == "failed"
    assert snapshot["error_code"] == ErrorCode.PERMISSION_DENIED.value
    assert snapshot["file_id"] is None and snapshot["generated_file_ids"] == []
    assert len(keys) == 1 and snapshot["object_keys"] == []
    _assert_attempt_objects_absent(root_storage, storage.bucket, keys)


def test_disabled_owner_with_no_sources_cannot_publish_after_render(
    runtime, database, ingestion_settings, monkeypatch
):
    username = _create_empty_source_owner(runtime)
    task_id, _ = _create_render_task(runtime, username=username)
    storage = runtime.worker.generation.storage
    keys = _track_attempt_keys(storage, monkeypatch)
    rendered = threading.Event()
    release_renderer = threading.Event()
    real_render = generation_worker_module.render_to_path

    def render_then_wait(*args: Any, **kwargs: Any) -> Any:
        result = real_render(*args, **kwargs)
        rendered.set()
        if not release_renderer.wait(timeout=30):
            raise TimeoutError("test did not release the completed render")
        return result

    monkeypatch.setattr(generation_worker_module, "render_to_path", render_then_wait)
    _, root_storage, _ = ingestion_settings
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_deliver_generation_event, runtime)
        try:
            assert rendered.wait(timeout=30)
            with Session(database.engine) as session, session.begin():
                owner = session.scalar(select(User).where(User.username == username))
                assert owner is not None and owner.role == "user"
                owner.status = "disabled"
            release_renderer.set()
        finally:
            release_renderer.set()
        assert future.result(timeout=30) is True

    snapshot = _task_snapshot(database, task_id, keys)
    # This assertion captures the account-state revalidation gap for empty
    # source_refs. Do not weaken it if it exposes the current worker behavior.
    assert snapshot["status"] == "failed"
    assert snapshot["error_code"] == ErrorCode.ACCOUNT_DISABLED.value
    assert snapshot["file_id"] is None and snapshot["generated_file_ids"] == []
    assert len(keys) == 1 and snapshot["object_keys"] == []
    _assert_attempt_objects_absent(root_storage, storage.bucket, keys)


def test_success_then_http_cancel_returns_success_and_event_replay_is_idempotent(
    runtime, database, ingestion_settings
):
    task_id, event_id = _create_render_task(runtime)
    storage = runtime.worker.generation.storage
    assert _deliver_generation_event(runtime)

    cancelled = runtime.client.post(
        f"/api/v1/generation-tasks/{task_id}/cancel", headers=runtime.admin
    )
    assert cancelled.status_code == 200, cancelled.text
    data = cancelled.json()["data"]
    assert data["status"] == "succeeded"
    assert data["complete"] is True
    assert data["file_id"] is not None

    runtime.run(runtime.worker.queue.publish, event_id)
    assert runtime.run(runtime.worker.run_once)
    snapshot = _task_snapshot(database, task_id, [])
    assert snapshot["status"] == "succeeded"
    assert snapshot["file_id"] is not None
    assert snapshot["generated_file_ids"] == [int(data["file_id"])]
    assert snapshot["storage_object_ids"] == [int(snapshot["file_id"])]
    with Session(database.engine) as session:
        generated = session.scalars(
            select(GeneratedFile).where(GeneratedFile.task_id == task_id)
        ).one()
        storage_object = session.get(StorageObject, generated.storage_object_id)
        assert storage_object is not None
        object_key = storage_object.object_key
        object_size = storage_object.size_bytes
    _, root_storage, _ = ingestion_settings
    assert root_storage.stat_object(storage.bucket, object_key).size == object_size
