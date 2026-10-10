import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from enterprise_mind.identity.models import Base, Department, User
from enterprise_mind.infrastructure.database import SCHEMA_REVISION
from enterprise_mind.infrastructure.settings import ConfigurationError

pytestmark = pytest.mark.integration
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _upgrade_to(database, revision: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with database.engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS infra"))
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _downgrade_to(database, revision: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def test_0010_active_snapshot_is_retired_by_identity_migration(database):
    _upgrade_to(database, "0010_database")
    with database.engine.begin() as connection:
        source_id = connection.scalar(
            text(
                """
                INSERT INTO integration.database_sources
                    (name, host, port, database_name, ssl_mode, secret_ref)
                VALUES ('legacy-source', 'legacy-db', 5432, 'legacy', 'disable',
                        'secret://legacy')
                RETURNING id
                """
            )
        )
        snapshot_id = connection.scalar(
            text(
                """
                INSERT INTO integration.schema_snapshots
                    (database_source_id, snapshot_no, status, captured_at)
                VALUES (:source_id, 1, 'active', now())
                RETURNING id
                """
            ),
            {"source_id": source_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO integration.schema_relations
                    (snapshot_id, schema_name, relation_name, relation_type)
                VALUES (:snapshot_id, 'legacy', 'orders', 'table')
                """
            ),
            {"snapshot_id": snapshot_id},
        )
        connection.execute(
            text(
                """
                UPDATE integration.database_sources
                SET active_snapshot_id = :snapshot_id
                WHERE id = :source_id
                """
            ),
            {"snapshot_id": snapshot_id, "source_id": source_id},
        )

    _upgrade_to(database, "head")
    with database.engine.connect() as connection:
        source = connection.execute(
            text(
                "SELECT active_snapshot_id, configuration_version "
                "FROM integration.database_sources WHERE id=:source_id"
            ),
            {"source_id": source_id},
        ).one()
        snapshot = connection.execute(
            text(
                "SELECT status, error_code FROM integration.schema_snapshots WHERE id=:snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        ).one()
        relation = connection.execute(
            text(
                "SELECT relation_oid, relation_signature "
                "FROM integration.schema_relations WHERE snapshot_id=:snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        ).one()

    assert source.active_snapshot_id is None
    assert source.configuration_version == 1
    assert snapshot == ("retired", "SCHEMA_REFRESH_REQUIRED")
    assert relation == (None, None)


def test_upgrade_from_0009_preserves_representative_v1_data_and_adds_database_schema(database):
    _upgrade_to(database, "0009_preview_rebuild")
    content = "保留的索引内容"
    with database.engine.begin() as connection:
        department_id = connection.scalar(
            text(
                """
                INSERT INTO iam.departments (name, name_key)
                VALUES ('迁移研发部', '迁移研发部')
                RETURNING id
                """
            )
        )
        user_id = connection.scalar(
            text(
                """
                INSERT INTO iam.users
                    (username, username_key, password_hash, role, department_id)
                VALUES (
                    '迁移管理员', '迁移管理员', 'synthetic-password-hash', 'admin', :department_id
                )
                RETURNING id
                """
            ),
            {"department_id": department_id},
        )
        knowledge_base_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.knowledge_bases (name, name_key, type, description)
                VALUES ('迁移知识库', '迁移知识库', 'public', '0009 legacy data')
                RETURNING id
                """
            )
        )
        folder_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.folders (knowledge_base_id, name, name_key)
                VALUES (:knowledge_base_id, '迁移目录', '迁移目录')
                RETURNING id
                """
            ),
            {"knowledge_base_id": knowledge_base_id},
        )
        storage_object_id = connection.scalar(
            text(
                """
                INSERT INTO infra.storage_objects
                    (bucket_name, object_key, object_type, size_bytes, content_type,
                     content_sha256, status)
                VALUES ('legacy-test', 'legacy/handbook.txt', 'original', 24,
                        'text/plain', repeat('a', 64), 'available')
                RETURNING id
                """
            )
        )
        document_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.documents
                    (knowledge_base_id, folder_id, name, name_key, description, created_by,
                     processing_status)
                VALUES (:knowledge_base_id, :folder_id, '迁移手册', '迁移手册',
                        'legacy document', :user_id, 'success')
                RETURNING id
                """
            ),
            {
                "knowledge_base_id": knowledge_base_id,
                "folder_id": folder_id,
                "user_id": user_id,
            },
        )
        revision_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.document_revisions
                    (document_id, revision_no, trigger_type, original_object_id,
                     original_file_name, file_extension, mime_type, size_bytes,
                     content_sha256, processing_status, initiated_by, activated_at)
                VALUES (:document_id, 1, 'initial_upload', :storage_object_id,
                        'handbook.txt', 'txt', 'text/plain', 24, repeat('b', 64),
                        'success', :user_id, now())
                RETURNING id
                """
            ),
            {
                "document_id": document_id,
                "storage_object_id": storage_object_id,
                "user_id": user_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE knowledge.documents
                SET active_revision_id = :revision_id
                WHERE id = :document_id
                """
            ),
            {"revision_id": revision_id, "document_id": document_id},
        )
        source_unit_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.source_units
                    (revision_id, unit_type, ordinal, label, extraction_status, extracted_text)
                VALUES (:revision_id, 'document', 0, '正文', 'success', :content)
                RETURNING id
                """
            ),
            {"revision_id": revision_id, "content": content},
        )
        element_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.content_elements
                    (source_unit_id, element_type, ordinal, content_text, token_estimate)
                VALUES (:source_unit_id, 'paragraph', 0, :content, 3)
                RETURNING id
                """
            ),
            {"source_unit_id": source_unit_id, "content": content},
        )
        anchor_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.source_anchors
                    (revision_id, source_unit_id, element_id, locator_type, locator,
                     source_text, source_text_sha256)
                VALUES (:revision_id, :source_unit_id, :element_id, 'text',
                        '{"offset": 0}'::jsonb, :content, repeat('c', 64))
                RETURNING id
                """
            ),
            {
                "revision_id": revision_id,
                "source_unit_id": source_unit_id,
                "element_id": element_id,
                "content": content,
            },
        )
        chunk_id = connection.scalar(
            text(
                """
                INSERT INTO knowledge.chunks
                    (revision_id, document_id, knowledge_base_id, ordinal, chunk_type,
                     content, search_tokens, search_vector, token_count, status)
                VALUES (:revision_id, :document_id, :knowledge_base_id, 0, 'text',
                        :content, '保留 索引 内容', to_tsvector('simple', :content), 3, 'ready')
                RETURNING id
                """
            ),
            {
                "revision_id": revision_id,
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "content": content,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO knowledge.chunk_anchors
                    (chunk_id, anchor_id, ordinal, content_start, content_end, quote_text)
                VALUES (:chunk_id, :anchor_id, 0, 0, char_length(:content), :content)
                """
            ),
            {"chunk_id": chunk_id, "anchor_id": anchor_id, "content": content},
        )
        conversation_id = connection.scalar(
            text(
                """
                INSERT INTO agent.conversations (owner_user_id, title, next_message_sequence)
                VALUES (:user_id, '迁移会话', 3)
                RETURNING id
                """
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO agent.messages
                    (conversation_id, sequence, role, content_text, status, completed_at)
                VALUES (:conversation_id, 1, 'user', '旧会话问题', 'completed', now())
                RETURNING id
                """
            ),
            {"conversation_id": conversation_id},
        )
        assistant_message_id = connection.scalar(
            text(
                """
                INSERT INTO agent.messages
                    (conversation_id, sequence, role, content_text, status, completed_at)
                VALUES (:conversation_id, 2, 'assistant', '旧会话回答', 'completed', now())
                RETURNING id
                """
            ),
            {"conversation_id": conversation_id},
        )
        answer_source_id = connection.scalar(
            text(
                """
                INSERT INTO agent.answer_sources
                    (message_id, ordinal, source_type, chunk_id, anchor_id, source_title,
                     quote_text, locator_snapshot, relevance)
                VALUES (:message_id, 1, 'knowledge', :chunk_id, :anchor_id, '迁移手册',
                        :content, '{"page": 1}'::jsonb, '{"score": 0.9}'::jsonb)
                RETURNING id
                """
            ),
            {
                "message_id": assistant_message_id,
                "chunk_id": chunk_id,
                "anchor_id": anchor_id,
                "content": content,
            },
        )
        legacy = {
            "user": tuple(
                connection.execute(
                    text(
                        "SELECT username, role, department_id, status FROM iam.users WHERE id=:id"
                    ),
                    {"id": user_id},
                ).one()
            ),
            "document": tuple(
                connection.execute(
                    text(
                        "SELECT knowledge_base_id, folder_id, name, processing_status, "
                        "active_revision_id FROM knowledge.documents WHERE id=:id"
                    ),
                    {"id": document_id},
                ).one()
            ),
            "revision": tuple(
                connection.execute(
                    text(
                        "SELECT document_id, revision_no, processing_status, initiated_by "
                        "FROM knowledge.document_revisions WHERE id=:id"
                    ),
                    {"id": revision_id},
                ).one()
            ),
            "chunk": tuple(
                connection.execute(
                    text(
                        "SELECT revision_id, document_id, knowledge_base_id, content, "
                        "status FROM knowledge.chunks WHERE id=:id"
                    ),
                    {"id": chunk_id},
                ).one()
            ),
            "conversation": tuple(
                connection.execute(
                    text(
                        "SELECT owner_user_id, title, next_message_sequence "
                        "FROM agent.conversations WHERE id=:id"
                    ),
                    {"id": conversation_id},
                ).one()
            ),
            "answer_source": tuple(
                connection.execute(
                    text(
                        "SELECT message_id, ordinal, source_type, chunk_id, anchor_id, "
                        "source_title, quote_text, locator_snapshot "
                        "FROM agent.answer_sources WHERE id=:id"
                    ),
                    {"id": answer_source_id},
                ).one()
            ),
        }

    _upgrade_to(database, "head")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            SCHEMA_REVISION
        )
        assert connection.scalar(text("SELECT to_regclass('integration.database_sources')"))
        assert connection.scalar(text("SELECT to_regclass('integration.schema_snapshots')"))
        assert connection.scalar(text("SELECT count(*) FROM integration.database_sources")) == 0
        assert legacy["user"] == tuple(
            connection.execute(
                text("SELECT username, role, department_id, status FROM iam.users WHERE id=:id"),
                {"id": user_id},
            ).one()
        )
        assert legacy["document"] == tuple(
            connection.execute(
                text(
                    "SELECT knowledge_base_id, folder_id, name, processing_status, "
                    "active_revision_id FROM knowledge.documents WHERE id=:id"
                ),
                {"id": document_id},
            ).one()
        )
        assert legacy["revision"] == tuple(
            connection.execute(
                text(
                    "SELECT document_id, revision_no, processing_status, initiated_by "
                    "FROM knowledge.document_revisions WHERE id=:id"
                ),
                {"id": revision_id},
            ).one()
        )
        assert legacy["chunk"] == tuple(
            connection.execute(
                text(
                    "SELECT revision_id, document_id, knowledge_base_id, content, status "
                    "FROM knowledge.chunks WHERE id=:id"
                ),
                {"id": chunk_id},
            ).one()
        )
        assert legacy["conversation"] == tuple(
            connection.execute(
                text(
                    "SELECT owner_user_id, title, next_message_sequence "
                    "FROM agent.conversations WHERE id=:id"
                ),
                {"id": conversation_id},
            ).one()
        )
        assert legacy["answer_source"] == tuple(
            connection.execute(
                text(
                    "SELECT message_id, ordinal, source_type, chunk_id, anchor_id, "
                    "source_title, quote_text, locator_snapshot "
                    "FROM agent.answer_sources WHERE id=:id"
                ),
                {"id": answer_source_id},
            ).one()
        )


def test_empty_upgrade_matches_models_and_repeat_does_not_touch_accounts(database, monkeypatch):
    assert database.initialize()
    with database.engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM infra.alembic_version"))
            == SCHEMA_REVISION
        )
        assert (
            connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
            == "0.8.6"
        )
        context = MigrationContext.configure(
            connection,
            opts={
                "include_schemas": True,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    type_ != "table" or obj.schema == "iam"
                ),
            },
        )
        assert compare_metadata(context, Base.metadata) == []
    with Session(database.engine) as session:
        before = session.execute(
            select(
                User.id,
                User.password_hash,
                User.credential_version,
                User.row_version,
                User.updated_at,
            )
        ).one()
    # A repeat never reads or needs the removed one-time secret, and ignores a changed username.
    database.admin_secret.unlink()
    monkeypatch.setenv("EM_BOOTSTRAP_ADMIN_USERNAME", "不应创建的管理员")
    assert not database.initialize()
    with Session(database.engine) as session:
        after = session.execute(
            select(
                User.id,
                User.password_hash,
                User.credential_version,
                User.row_version,
                User.updated_at,
            )
        ).one()
        assert before == after
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_phase10_chart_migration_round_trip_and_fail_closed_downgrade(database):
    """0013 round-trips empty state and refuses to hide chart rows on downgrade."""

    assert database.initialize()
    _downgrade_to(database, "0012_phase9")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            "0012_phase9"
        )
    _upgrade_to(database, "head")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            SCHEMA_REVISION
        )
        owner_id = connection.scalar(text("SELECT id FROM iam.users WHERE role='admin' LIMIT 1"))

    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent.generation_tasks
                    (owner_user_id, kind, format, scope, idempotency_key, request_fingerprint)
                VALUES (:owner_id, 'chart_render', 'png', 'all_matching',
                        'migration-chart-row', repeat('a', 64))
                """
            ),
            {"owner_id": owner_id},
        )

    with pytest.raises(DBAPIError):
        _downgrade_to(database, "0012_phase9")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            SCHEMA_REVISION
        )


def test_phase10_chart_bundle_downgrade_refuses_multiple_files(database):
    """0014 cannot restore one-file-per-task while a chart bundle exists."""

    assert database.initialize()
    with database.engine.connect() as connection:
        owner_id = connection.scalar(text("SELECT id FROM iam.users WHERE role='admin' LIMIT 1"))
    with database.engine.begin() as connection:
        task_id = connection.scalar(
            text(
                """
                INSERT INTO agent.generation_tasks
                    (owner_user_id, kind, format, scope, idempotency_key, request_fingerprint)
                VALUES (:owner_id, 'chart_render', 'png', 'all_matching',
                        'migration-bundle-task', repeat('b', 64))
                RETURNING id
                """
            ),
            {"owner_id": owner_id},
        )
        for index, file_format in enumerate(("png", "pdf", "xlsx", "csv"), 1):
            object_id = connection.scalar(
                text(
                    """
                    INSERT INTO infra.storage_objects
                        (bucket_name, object_key, object_type, size_bytes, content_type,
                         content_sha256, status)
                    VALUES ('test-bucket', :object_key, 'export', 1, :content_type,
                            repeat('c', 64), 'available')
                    RETURNING id
                    """
                ),
                {
                    "object_key": f"migration-bundle-{index}",
                    "content_type": {
                        "png": "image/png",
                        "pdf": "application/pdf",
                        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "csv": "text/csv",
                    }[file_format],
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agent.generated_files
                        (task_id, owner_user_id, storage_object_id, file_name, file_format,
                         generation_kind, source_refs, content_sha256, size_bytes)
                    VALUES (:task_id, :owner_id, :object_id, :file_name, :file_format,
                            'chart_render', '[]'::jsonb, repeat('c', 64), 1)
                    """
                ),
                {
                    "task_id": task_id,
                    "owner_id": owner_id,
                    "object_id": object_id,
                    "file_name": f"chart.{file_format}",
                    "file_format": file_format,
                },
            )

    with pytest.raises(RuntimeError, match="multiple artifacts"):
        _downgrade_to(database, "0013_phase10_charts")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            SCHEMA_REVISION
        )


@pytest.mark.parametrize("failure", ["username", "password", "weak_password"])
def test_bad_bootstrap_rolls_back_entire_empty_install(database, monkeypatch, failure):
    if failure == "username":
        monkeypatch.delenv("EM_BOOTSTRAP_ADMIN_USERNAME")
    elif failure == "password":
        monkeypatch.delenv("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE")
    else:
        database.admin_secret.write_text("short", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        database.initialize()
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('iam.users')")) is None
        assert connection.scalar(text("SELECT to_regclass('infra.alembic_version')")) is None


def test_nonempty_database_without_admin_is_not_escalated(database):
    database.initialize()
    with Session(database.engine) as session, session.begin():
        department = Department(name="研发", name_key="研发")
        session.add(department)
        session.flush()
        admin = session.scalars(select(User)).one()
        admin.role, admin.department_id = "user", department.id
    with pytest.raises(ConfigurationError):
        database.initialize()
    with Session(database.engine) as session:
        assert session.scalars(select(User)).one().role == "user"


def test_multiple_admins_are_allowed_and_initialization_preserves_them(database):
    database.initialize()
    with Session(database.engine) as session, session.begin():
        first = session.scalars(select(User)).one()
        session.add(
            User(
                username="第二位管理员",
                username_key="第二位管理员",
                password_hash=first.password_hash,
                role="admin",
                department_id=None,
            )
        )
    database.admin_secret.unlink()
    assert not database.initialize()
    with Session(database.engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(User).where(User.role == "admin")) == 2
        )


def test_parallel_initializers_create_exactly_one_admin(database):
    env = os.environ.copy()
    env.update(
        EM_ENV="test",
        EM_POSTGRES_HOST=database.settings.postgres_host,
        EM_POSTGRES_PORT=str(database.settings.postgres_port),
        EM_POSTGRES_DATABASE=database.settings.postgres_database,
        EM_POSTGRES_USER=database.settings.postgres_user,
        EM_POSTGRES_PASSWORD_FILE=str(database.settings.postgres_password_file),
        EM_APPLICATION_CONFIG=str(database.settings.application_config),
    )

    def run():
        return subprocess.run(
            [sys.executable, "-m", "enterprise_mind.cli", "migrate", "--backend-root", "."],
            env=env,
            capture_output=True,
            timeout=60,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [future.result() for future in futures]
    assert [result.returncode for result in results] == [0, 0]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE iam.users SET role='superadmin'",
        "UPDATE iam.users SET role='user', department_id=NULL",
        "UPDATE iam.users SET status='deleted'",
        "UPDATE iam.users SET row_version=0",
        "UPDATE iam.users SET credential_version=0",
        "UPDATE iam.users SET department_id=99999999",
        "UPDATE iam.users SET username_key=''",
    ],
)
def test_database_rejects_invalid_identity_state(database, mutation):
    database.initialize()
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(text(mutation))
    with Session(database.engine) as session:
        admin = session.scalars(select(User)).one()
        assert (admin.role, admin.status, admin.row_version, admin.credential_version) == (
            "admin",
            "active",
            1,
            1,
        )


def test_unique_names_foreign_key_restrict_and_identity_always(database):
    database.initialize()
    with Session(database.engine) as session, session.begin():
        department = Department(name="研发", name_key="研发")
        session.add(department)
        session.flush()
        admin = session.scalars(select(User)).one()
        admin.department_id = department.id
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(text("DELETE FROM iam.departments"))
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO iam.departments (name,name_key) VALUES ('研发','研发')")
        )
    with pytest.raises(ProgrammingError) as error, database.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO iam.departments (id,name,name_key) VALUES (99,'x','x')")
        )
    assert error.value.orig.sqlstate == "428C9"
