"""Real PostgreSQL migration coverage for generation execution leases."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import enterprise_mind.database.models as database_models  # noqa: F401
import enterprise_mind.knowledge.pending_models as pending_models  # noqa: F401
from enterprise_mind.agent import models as agent_models  # noqa: F401
from enterprise_mind.agent.generation_models import GenerationTask  # noqa: F401
from enterprise_mind.charts import models as chart_models  # noqa: F401
from enterprise_mind.database import export_models  # noqa: F401
from enterprise_mind.identity.models import Base
from enterprise_mind.infrastructure import models as infrastructure_models  # noqa: F401
from enterprise_mind.infrastructure.database import SCHEMA_REVISION
from enterprise_mind.ingestion import models as ingestion_models  # noqa: F401
from enterprise_mind.knowledge import models as knowledge_models  # noqa: F401
from enterprise_mind.memory import models as memory_models  # noqa: F401
from enterprise_mind.rag import models as rag_models  # noqa: F401

pytestmark = pytest.mark.integration
BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_0014 = "0014_phase10_chart_bundle"


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


def _insert_owner(connection) -> int:
    return connection.scalar(
        text(
            """
            INSERT INTO iam.users (username, username_key, password_hash, role)
            VALUES ('lease-migration-owner', 'lease-migration-owner', 'unused', 'admin')
            RETURNING id
            """
        )
    )


def _insert_running_task(connection, owner_id: int, idempotency_key: str) -> int:
    return connection.scalar(
        text(
            """
            INSERT INTO agent.generation_tasks
                (owner_user_id, kind, format, idempotency_key, request_fingerprint,
                 status, phase)
            VALUES (:owner_id, 'content_render', 'md', :idempotency_key,
                    repeat('a', 64), 'running', 'render_content')
            RETURNING id
            """
        ),
        {"owner_id": owner_id, "idempotency_key": idempotency_key},
    )


def test_0015_upgrade_preserves_legacy_running_task_and_matches_model(database):
    _upgrade_to(database, REVISION_0014)
    with database.engine.begin() as connection:
        owner_id = _insert_owner(connection)
        task_id = _insert_running_task(connection, owner_id, "lease-migration-legacy")

    _upgrade_to(database, "head")

    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            SCHEMA_REVISION
        )
        task = connection.execute(
            text(
                """
                SELECT status, phase, execution_owner, execution_lease_expires_at,
                       execution_attempt_count
                FROM agent.generation_tasks WHERE id=:task_id
                """
            ),
            {"task_id": task_id},
        ).one()
        assert tuple(task) == ("running", "render_content", None, None, 0)

        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("generation_tasks", schema="agent")
        }
        assert columns["execution_owner"]["nullable"] is True
        assert columns["execution_owner"]["default"] is None
        assert columns["execution_lease_expires_at"]["nullable"] is True
        assert columns["execution_lease_expires_at"]["default"] is None
        assert columns["execution_attempt_count"]["nullable"] is False
        assert columns["execution_attempt_count"]["default"] == "0"

        context = MigrationContext.configure(
            connection,
            opts={
                "include_schemas": True,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    type_ != "table" or (obj.schema == "agent" and obj.name == "generation_tasks")
                ),
            },
        )
        assert compare_metadata(context, Base.metadata) == []

    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE agent.generation_tasks SET execution_attempt_count=-1 WHERE id=:id"),
                {"id": task_id},
            )


def test_0015_downgrade_to_0014_and_reupgrade_keeps_running_task_reclaimable(database):
    _upgrade_to(database, REVISION_0014)
    with database.engine.begin() as connection:
        owner_id = _insert_owner(connection)
        task_id = _insert_running_task(connection, owner_id, "lease-migration-roundtrip")

    _upgrade_to(database, "head")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE agent.generation_tasks
                SET execution_owner='retiring-worker',
                    execution_lease_expires_at=now() + interval '1 minute',
                    execution_attempt_count=4
                WHERE id=:task_id
                """
            ),
            {"task_id": task_id},
        )

    _downgrade_to(database, REVISION_0014)
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            REVISION_0014
        )
        assert connection.execute(
            text("SELECT status, phase FROM agent.generation_tasks WHERE id=:task_id"),
            {"task_id": task_id},
        ).one() == ("running", "render_content")
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("generation_tasks", schema="agent")
        }
        assert "execution_owner" not in columns
        assert "execution_lease_expires_at" not in columns
        assert "execution_attempt_count" not in columns

    _upgrade_to(database, "head")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM infra.alembic_version")) == (
            SCHEMA_REVISION
        )
        task = connection.execute(
            text(
                """
                SELECT status, phase, execution_owner, execution_lease_expires_at,
                       execution_attempt_count
                FROM agent.generation_tasks WHERE id=:task_id
                """
            ),
            {"task_id": task_id},
        ).one()
        assert tuple(task) == ("running", "render_content", None, None, 0)
