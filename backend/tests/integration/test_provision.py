import asyncio
import secrets

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from conftest import BACKEND
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.app import create_app
from enterprise_mind.identity.models import Base, User
from enterprise_mind.infrastructure.migrate import bootstrap_admin, migrate
from enterprise_mind.infrastructure.provision import (
    APP,
    BACKUP,
    CHECKPOINT,
    MIGRATOR,
    OWNER,
    TRIGGER_FUNCTIONS,
    provision,
)
from enterprise_mind.infrastructure.settings import ApplicationConfig, ConfigurationError

pytestmark = pytest.mark.integration
ROLES = (OWNER, MIGRATOR, APP, BACKUP, CHECKPOINT)


@pytest.mark.parametrize(
    "initial_state",
    [
        "empty",
        "stage1",
        "stage2",
        "stage3",
        "stage4",
        "stage5",
        "stage6",
        "attachments",
        "preview_rebuild",
        "absent",
        "ingestion",
    ],
)
def test_provision_ownership_migration_runtime_and_backup(
    database, tmp_path, initial_state, request
):
    upgrade_from_stage1 = initial_state in {
        "stage1",
        "stage2",
        "stage3",
        "stage4",
        "stage5",
        "stage6",
        "attachments",
        "preview_rebuild",
    }
    services = (
        request.getfixturevalue("ingestion_settings")[0] if initial_state == "ingestion" else None
    )
    if upgrade_from_stage1:
        with database.engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA infra"))
            config = Config(str(BACKEND / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(
                config,
                {
                    "stage1": "0001_identity",
                    "stage2": "0002_organization",
                    "stage3": "0003_ingestion",
                    "stage4": "0004_retrieval",
                    "stage5": "0005_agent",
                    "stage6": "0006_lifecycle",
                    "attachments": "0007_attachments",
                    "preview_rebuild": "0009_preview_rebuild",
                }[initial_state],
            )
            with Session(bind=connection, join_transaction_mode="rollback_only") as session:
                bootstrap_admin(session, ApplicationConfig())
            original = connection.execute(
                text(
                    "SELECT id, password_hash, credential_version, row_version, created_at "
                    "FROM iam.users"
                )
            ).one()
    with database.engine.connect() as connection:
        # Never adopt or clean roles left by another deployment/test run.
        assert not connection.scalar(
            text("SELECT count(*) FROM pg_roles WHERE rolname=ANY(:roles)"), {"roles": list(ROLES)}
        )
    passwords = {role: secrets.token_urlsafe(32) for role in ROLES if role != OWNER}
    files = {}
    for role, value in passwords.items():
        files[role] = tmp_path / role
        files[role].write_text(value, encoding="utf-8")
    if initial_state == "absent":
        maintenance = create_engine(
            database.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
            poolclass=NullPool,
        )
        try:
            with maintenance.connect() as connection:
                # Only this test's empty random database is removed; provision recreates it.
                assert database.url.database.startswith("em_test_")
                name = connection.dialect.identifier_preparer.quote_identifier(
                    database.url.database
                )
                connection.execute(text(f"DROP DATABASE {name}"))
        finally:
            maintenance.dispose()
    try:
        provision(database.url, files)
        # Idempotent provisioning must not need/reset existing credential files.
        provision(database.url, {})
        migrator_url = database.url.set(username=MIGRATOR, password=passwords[MIGRATOR])
        assert migrate(migrator_url, ApplicationConfig(), BACKEND) is (not upgrade_from_stage1)
        assert not migrate(migrator_url, ApplicationConfig(), BACKEND)
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(initialize_checkpoints(migrator_url))
            runner.run(initialize_checkpoints(migrator_url))
        with database.engine.connect() as connection:
            if upgrade_from_stage1:
                assert (
                    connection.execute(
                        text(
                            "SELECT id, password_hash, credential_version, row_version, created_at "
                            "FROM iam.users"
                        )
                    ).one()
                    == original
                )
            owners = (
                connection.execute(
                    text(
                        "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname IN "
                        "('iam','knowledge','agent','integration','infra','langgraph_checkpoint') "
                        "AND c.relkind IN ('r','S')"
                    )
                )
                .scalars()
                .all()
            )
            assert owners and set(owners) == {OWNER}
            function_owners = connection.execute(
                text(
                    "SELECT n.nspname || '.' || p.proname, pg_get_userbyid(p.proowner) "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname || '.' || p.proname = ANY(:names)"
                ),
                {"names": sorted(TRIGGER_FUNCTIONS)},
            ).all()
            assert {name for name, _ in function_owners} == TRIGGER_FUNCTIONS
            assert {owner for _, owner in function_owners} == {OWNER}
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_schemas": True,
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        type_ != "table"
                        or (
                            obj.schema in {"iam", "knowledge", "infra", "agent"}
                            and name != "alembic_version"
                        )
                    ),
                },
            )
            assert compare_metadata(context, Base.metadata) == []
        runtime_settings = database.settings.model_copy(
            update={"postgres_user": APP, "postgres_password_file": files[APP]}
        )
        app = create_app(runtime_settings, services)
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "初始管理员", "password": database.admin_password},
            )
            assert response.status_code == 200, response.text
            headers = {
                "Authorization": "Bearer " + response.json()["data"]["access_token"],
                "Idempotency-Key": "app-create",
            }
            response = client.post(
                "/api/v1/admin/departments", headers=headers, json={"name": "最小权限验证"}
            )
            assert response.status_code == 201, response.text
            department_id = response.json()["data"]["id"]
            response = client.patch(
                "/api/v1/admin/departments/" + department_id,
                headers=headers,
                json={"row_version": 1, "name": "已更新"},
            )
            assert response.status_code == 200, response.text
            assert (
                client.delete(
                    "/api/v1/admin/departments/" + department_id,
                    headers=headers,
                    params={"row_version": 2},
                ).status_code
                == 204
            )
            response = client.post(
                "/api/v1/knowledge-bases",
                headers={**headers, "Idempotency-Key": "kb-create"},
                json={"name": "公共资料", "type": "public"},
            )
            assert response.status_code == 201, response.text
            if services is not None:
                from enterprise_mind.ingestion.dispatch import Dispatcher
                from enterprise_mind.ingestion.worker import IngestionWorker

                base_id = response.json()["data"]["id"]
                upload = client.post(
                    f"/api/v1/knowledge-bases/{base_id}/document-uploads",
                    headers={**headers, "Idempotency-Key": "app-upload"},
                    files=[("files", ("runtime.txt", b"Runtime role content", "text/plain"))],
                )
                assert upload.status_code == 202, upload.text
                document_id = upload.json()["data"]["items"][0]["document_id"]
                dispatcher = Dispatcher(app.state.sessions, app.state.queue, app.state.config)
                worker = IngestionWorker(
                    app.state.sessions, app.state.storage, app.state.queue, app.state.config.worker
                )
                assert client.portal.call(dispatcher.dispatch_once) == 1
                assert client.portal.call(worker.run_once)
                progress = client.get(
                    f"/api/v1/documents/{document_id}/processing", headers=headers
                )
                assert progress.json()["data"]["current_stage"] == "chunk", progress.text
                from test_rag_pipeline import TestEmbedding

                from enterprise_mind.ingestion.models import OutboxEvent
                from enterprise_mind.rag.config import RAGConfig
                from enterprise_mind.rag.indexing import IndexingService

                worker = IngestionWorker(
                    app.state.sessions,
                    app.state.storage,
                    app.state.queue,
                    app.state.config.worker,
                    IndexingService(app.state.sessions, RAGConfig(), TestEmbedding()),
                )
                with Session(database.engine) as session:
                    event_id = session.scalars(select(OutboxEvent.id)).one()
                assert client.portal.call(worker.handle_event, event_id)
                progress = client.get(
                    f"/api/v1/documents/{document_id}/processing", headers=headers
                )
                assert progress.json()["data"]["processing_status"] == "success", progress.text
                preview = client.get(f"/api/v1/documents/{document_id}/preview", headers=headers)
                assert preview.status_code == 200 and preview.json()["data"]["ready"]
                content_url = preview.json()["data"]["artifacts"][0]["content_url"]
                assert client.get(content_url, headers=headers).status_code == 200
                assert client.get("/health/ready").json()["stage"] == "ingestion_foundation"
                folder = client.post(
                    f"/api/v1/knowledge-bases/{base_id}/folders",
                    headers={**headers, "Idempotency-Key": "app-folder"},
                    json={"name": "Lifecycle"},
                )
                assert folder.status_code == 201, folder.text
                folder_id = folder.json()["data"]["id"]
                moved = client.patch(
                    f"/api/v1/documents/{document_id}",
                    headers=headers,
                    json={"row_version": 1, "folder_id": folder_id},
                )
                assert moved.status_code == 200, moved.text
                for operation, version in (("deletion", 1), ("purge", 2)):
                    action = client.post(
                        f"/api/v1/folders/{folder_id}/{operation}-requests",
                        headers={**headers, "Idempotency-Key": f"app-{operation}"},
                        json={"row_version": version},
                    )
                    assert action.status_code == 200, action.text
                    identifier = action.json()["data"]["id"]
                    accepted = client.post(
                        f"/api/v1/pending-actions/{identifier}/responses",
                        headers={**headers, "Idempotency-Key": f"app-response-{operation}"},
                        json={"decision": "accept"},
                    )
                    assert accepted.status_code == 200, accepted.text
                with Session(database.engine) as session:
                    purge_id = session.scalar(
                        select(OutboxEvent.id).where(
                            OutboxEvent.event_type == "document.purge.requested"
                        )
                    )
                assert not client.portal.call(worker.handle_event, purge_id)
                with Session(database.engine) as session:
                    object_events = list(
                        session.scalars(
                            select(OutboxEvent.id).where(
                                OutboxEvent.event_type == "storage.delete.requested"
                            )
                        )
                    )
                assert len(object_events) == 2
                for object_event in object_events:
                    assert client.portal.call(worker.handle_event, object_event)
                assert client.portal.call(worker.handle_event, purge_id)
                from enterprise_mind.agent import attachments
                from enterprise_mind.agent.service import actor
                from enterprise_mind.knowledge.lifecycle import write_guard

                conversation = client.post(
                    "/api/v1/conversations",
                    headers={**headers, "Idempotency-Key": "app-chat"},
                    json={"title": "运行账号附件"},
                )
                assert conversation.status_code == 201, conversation.text
                conversation_id = conversation.json()["data"]["id"]
                staged = client.post(
                    f"/api/v1/conversations/{conversation_id}/attachments",
                    headers={**headers, "Idempotency-Key": "app-attachment"},
                    files=[("files", ("attached.txt", b"Runtime attached content", "text/plain"))],
                )
                assert staged.status_code == 201, staged.text
                staged_id = staged.json()["data"]["items"][0]["staged_upload_id"]
                message = client.post(
                    f"/api/v1/conversations/{conversation_id}/messages",
                    headers={**headers, "Idempotency-Key": "app-attachment-message"},
                    json={"content_text": "保存附件", "staged_upload_ids": [staged_id]},
                )
                assert message.status_code == 202, message.text

                async def consume_with_app_role():
                    async with app.state.sessions() as session, session.begin():
                        identity = await write_guard(
                            session, await actor(session, database.user_id())
                        )
                        obj = await attachments.for_run(
                            session, identity, int(message.json()["data"]["id"]), int(staged_id)
                        )
                        return await attachments.consume_upload(
                            session,
                            identity,
                            int(message.json()["data"]["id"]),
                            {"knowledge_base_id": base_id, "files": [attachments.metadata(obj)]},
                        )

                consumed = client.portal.call(consume_with_app_role)
                assert consumed["items"][0]["status"] == "accepted"
            response = client.post(
                "/api/v1/admin/users",
                headers={**headers, "Idempotency-Key": "user-create"},
                json={
                    "username": "另一管理员",
                    "password": database.admin_password,
                    "role": "admin",
                },
            )
            assert response.status_code == 201, response.text
            response = client.post(
                "/api/v1/admin/users/" + response.json()["data"]["id"] + "/password-reset",
                headers=headers,
                json={"row_version": 1, "new_password": database.admin_password + "x"},
            )
            assert response.status_code == 200, response.text
        for role in (APP, BACKUP, CHECKPOINT):
            engine = create_engine(
                database.url.set(username=role, password=passwords[role]),
                hide_parameters=True,
                poolclass=NullPool,
            )
            try:
                with engine.connect() as connection:
                    if role == CHECKPOINT:
                        assert (
                            connection.scalar(
                                text(
                                    "SELECT count(*) FROM "
                                    "langgraph_checkpoint.checkpoint_migrations"
                                )
                            )
                            > 0
                        )
                        connection.execute(
                            text(
                                "DELETE FROM langgraph_checkpoint.checkpoints "
                                "WHERE thread_id='missing'"
                            )
                        )
                    else:
                        assert connection.execute(select(User.id)).first()
                    if role == APP:
                        assert connection.scalar(
                            text(
                                "SELECT has_schema_privilege(current_user, 'integration', 'USAGE')"
                            )
                        )
                        integration_tables = connection.execute(
                            text(
                                "SELECT n.nspname || '.' || c.relname "
                                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                "WHERE n.nspname='integration' AND c.relkind IN ('r','p') "
                                "ORDER BY c.relname"
                            )
                        ).scalars()
                        for table in integration_tables:
                            assert connection.scalar(
                                text(
                                    "SELECT has_table_privilege(current_user, :table_name, "
                                    "'SELECT,INSERT,UPDATE,DELETE')"
                                ),
                                {"table_name": table},
                            )
                        assert (
                            connection.scalar(
                                text("SELECT count(*) FROM integration.database_sources")
                            )
                            == 0
                        )
                    if role == BACKUP:
                        assert (
                            connection.scalar(
                                text(
                                    "SELECT count(*) FROM "
                                    "langgraph_checkpoint.checkpoint_migrations"
                                )
                            )
                            > 0
                        )
                denied = [
                    "CREATE TABLE iam.forbidden(id int)",
                    "CREATE TABLE public.forbidden(id int)",
                    "CREATE TEMP TABLE forbidden(id int)",
                    f"SET ROLE {OWNER}",
                    "CREATE ROLE forbidden_role",
                ]
                if role == APP:
                    denied += [
                        "DELETE FROM iam.users",
                        "UPDATE infra.idempotency_records SET operation_name='changed'",
                        "UPDATE infra.alembic_version SET version_num='changed'",
                        "SELECT * FROM langgraph_checkpoint.checkpoints",
                    ]
                elif role == CHECKPOINT:
                    denied += [
                        "SELECT * FROM iam.users",
                        "SELECT * FROM agent.messages",
                        "CREATE TABLE langgraph_checkpoint.forbidden(id int)",
                    ]
                else:
                    denied += [
                        "UPDATE iam.users SET username='changed'",
                        "DELETE FROM iam.departments",
                    ]
                for statement in denied:
                    with pytest.raises(ProgrammingError), engine.begin() as connection:
                        connection.execute(text(statement))
            finally:
                engine.dispose()
        # Reject unsafe pre-existing role settings without resetting credentials.
        with database.engine.begin() as connection:
            connection.execute(text(f"ALTER ROLE {APP} CREATEDB"))
        with pytest.raises(ConfigurationError):
            provision(database.url, {})
    finally:
        with database.engine.begin() as connection:
            owner_exists = connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": OWNER}
            )
            if owner_exists:
                # Only roles verified absent above and this random test database are involved.
                connection.execute(text(f'REASSIGN OWNED BY {OWNER} TO "em_test_owner"'))
            for role in (CHECKPOINT, APP, BACKUP, MIGRATOR, OWNER):
                if connection.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
                ):
                    connection.execute(text(f"DROP OWNED BY {role}"))
                    connection.execute(text(f"DROP ROLE {role}"))
