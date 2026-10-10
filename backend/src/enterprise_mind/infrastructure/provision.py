"""Explicit database provisioning, run only with the initialization credential."""

from pathlib import Path

from sqlalchemy import URL, Connection, create_engine, text
from sqlalchemy.pool import NullPool

from enterprise_mind.infrastructure.settings import ConfigurationError, read_secret

OWNER = "enterprise_mind_owner"
MIGRATOR = "enterprise_mind_migrator"
APP = "enterprise_mind_app"
BACKUP = "enterprise_mind_backup"
CHECKPOINT = "enterprise_mind_checkpoint"
SCHEMAS = ("iam", "knowledge", "agent", "integration", "infra", "langgraph_checkpoint")
TABLES = {
    "integration.database_sources",
    "integration.schema_snapshots",
    "integration.schema_relations",
    "integration.schema_columns",
    "integration.schema_constraints",
    "integration.relation_policies",
    "integration.relation_department_grants",
    "iam.users",
    "iam.departments",
    "knowledge.knowledge_bases",
    "knowledge.deletion_batches",
    "infra.idempotency_records",
    "infra.alembic_version",
    "knowledge.folders",
    "knowledge.documents",
    "knowledge.document_revisions",
    "knowledge.document_artifacts",
    "knowledge.processing_tasks",
    "knowledge.processing_units",
    "knowledge.source_units",
    "knowledge.content_elements",
    "knowledge.source_anchors",
    "knowledge.chunks",
    "knowledge.chunk_anchors",
    "knowledge.chunk_embeddings",
    "infra.storage_objects",
    "infra.outbox_events",
    "agent.conversations",
    "agent.messages",
    "agent.conversation_summaries",
    "agent.user_memory_states",
    "agent.personal_memories",
    "agent.content_drafts",
    "agent.database_export_plans",
    "agent.generation_tasks",
    "agent.generated_files",
    "agent.agent_runs",
    "agent.tool_calls",
    "agent.answer_sources",
    "agent.pending_actions",
    "agent.pending_action_targets",
    "langgraph_checkpoint.checkpoints",
    "langgraph_checkpoint.checkpoint_blobs",
    "langgraph_checkpoint.checkpoint_writes",
    "langgraph_checkpoint.checkpoint_migrations",
}
TRIGGER_FUNCTIONS = {
    "knowledge.validate_chunk_anchor",
    "knowledge.protect_indexed_source",
    "agent.validate_run_messages",
    "agent.validate_answer_source",
    "integration.validate_database_source_snapshot",
}
PROVISION_LOCK = 4617078203315146319


def quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(identifier)


def provision_roles(connection: Connection, secrets: dict[str, Path | None]) -> None:
    checkpoint_exists = connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": CHECKPOINT}
    )
    roles = (OWNER, MIGRATOR, APP, BACKUP) + (
        (CHECKPOINT,) if secrets.get(CHECKPOINT) or checkpoint_exists else ()
    )
    for role in roles:
        current = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls, rolinherit FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        ).one_or_none()
        if current is not None:
            if tuple(current) != (role != OWNER, False, False, False, False, False, False):
                raise ConfigurationError("已有部署角色的安全属性不符合要求，请受控修复。")
            continue
        connection.execute(
            text(
                f"CREATE ROLE {quote(connection, role)} "
                f"{'NOLOGIN' if role == OWNER else 'LOGIN'} NOINHERIT "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )
        if role != OWNER:
            path = secrets.get(role)
            if path is None:
                raise ConfigurationError("创建部署登录角色需要各自的密码 Secret。")
            # Bind the secret as a parameter. It never appears in SQL text or command arguments.
            connection.execute(
                text("SELECT set_config('enterprise_mind.provision_password', :password, true)"),
                {"password": read_secret(path).get_secret_value()},
            )
            connection.execute(
                text(
                    "DO $body$ BEGIN EXECUTE format('ALTER ROLE %I PASSWORD %L', "
                    f"'{role}', current_setting('enterprise_mind.provision_password')); END $body$"
                )
            )
    memberships = connection.execute(
        text(
            "SELECT member.rolname, parent.rolname, m.admin_option, m.inherit_option, m.set_option "
            "FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member "
            "JOIN pg_roles parent ON parent.oid=m.roleid "
            "WHERE member.rolname = ANY(:roles)"
        ),
        {"roles": list(roles)},
    ).all()
    if any(tuple(row) != (MIGRATOR, OWNER, False, False, True) for row in memberships):
        raise ConfigurationError("部署角色存在未授权的角色继承，请受控修复。")
    connection.execute(
        text(f"GRANT {OWNER} TO {MIGRATOR} WITH INHERIT FALSE, SET TRUE, ADMIN FALSE")
    )


def grant_runtime(connection: Connection) -> None:
    for schema in SCHEMAS:
        connection.execute(text(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC"))
        connection.execute(text(f"REVOKE ALL ON SCHEMA {schema} FROM {APP}, {BACKUP}"))
        connection.execute(
            text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC, {APP}, {BACKUP}")
        )
        connection.execute(
            text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC, {APP}, {BACKUP}")
        )
        connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {BACKUP}"))
        connection.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {BACKUP}"))
        connection.execute(text(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {BACKUP}"))
        connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
                f"GRANT SELECT ON TABLES TO {BACKUP}"
            )
        )
    connection.execute(text(f"GRANT USAGE ON SCHEMA iam, knowledge, infra TO {APP}"))
    connection.execute(text(f"GRANT USAGE ON SCHEMA agent TO {APP}"))
    if connection.scalar(text("SELECT to_regclass('integration.database_sources')")):
        connection.execute(text(f"GRANT USAGE ON SCHEMA integration TO {APP}"))
        connection.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA integration TO {APP}"
            )
        )
        connection.execute(text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA integration TO {APP}"))
    connection.execute(
        text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent TO {APP}")
    )
    connection.execute(text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA agent TO {APP}"))
    connection.execute(
        text(f"GRANT SELECT ON infra.alembic_version, knowledge.deletion_batches TO {APP}")
    )
    connection.execute(
        text(f"GRANT SELECT, INSERT, UPDATE ON iam.users, knowledge.knowledge_bases TO {APP}")
    )
    connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON iam.departments TO {APP}"))
    connection.execute(text(f"GRANT SELECT, INSERT ON infra.idempotency_records TO {APP}"))
    connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.folders, "
            f"knowledge.deletion_batches TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT DELETE ON knowledge.knowledge_bases, knowledge.documents, "
            "knowledge.document_revisions, knowledge.processing_tasks, knowledge.processing_units, "
            "knowledge.chunks, knowledge.chunk_embeddings, knowledge.chunk_anchors, "
            f"infra.storage_objects TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT USAGE ON SEQUENCE knowledge.folders_id_seq, "
            f"knowledge.deletion_batches_id_seq TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE ON knowledge.chunks, knowledge.chunk_anchors, "
            f"knowledge.chunk_embeddings TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT USAGE ON SEQUENCE knowledge.chunks_id_seq, knowledge.chunk_anchors_id_seq, "
            f"knowledge.chunk_embeddings_id_seq TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE ON knowledge.documents, knowledge.document_revisions, "
            "knowledge.processing_tasks, knowledge.processing_units, "
            f"infra.storage_objects TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.document_artifacts, "
            "knowledge.source_units, "
            f"knowledge.content_elements, knowledge.source_anchors, infra.outbox_events TO {APP}"
        )
    )
    connection.execute(
        text(
            "GRANT USAGE ON SEQUENCE knowledge.document_artifacts_id_seq, "
            "knowledge.documents_id_seq, "
            "knowledge.document_revisions_id_seq, knowledge.processing_tasks_id_seq, "
            "knowledge.processing_units_id_seq, knowledge.source_units_id_seq, "
            "knowledge.content_elements_id_seq, knowledge.source_anchors_id_seq, "
            f"infra.storage_objects_id_seq, infra.outbox_events_id_seq TO {APP}"
        )
    )
    connection.execute(
        text(
            f"GRANT USAGE ON SEQUENCE iam.users_id_seq, iam.departments_id_seq, "
            f"knowledge.knowledge_bases_id_seq, infra.idempotency_records_id_seq TO {APP}"
        )
    )


def provision(url: URL, secrets: dict[str, Path | None]) -> None:
    if (
        not url.database
        or len(url.database.encode()) > 63
        or url.database in {"postgres", "template0", "template1"}
    ):
        raise ConfigurationError("必须指定独立的 EnterpriseMind 数据库。")
    maintenance = create_engine(
        url.set(database="postgres"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    target = create_engine(
        url, poolclass=NullPool, hide_parameters=True, connect_args={"connect_timeout": 5}
    )
    try:
        with maintenance.connect() as connection:
            if not connection.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            ):
                raise ConfigurationError("数据库预配置只能由初始化超级用户执行。")
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": PROVISION_LOCK})
            connection.commit()
            try:
                with connection.begin():
                    provision_roles(connection, secrets)
                database_exists = connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": url.database}
                )
                connection.commit()
                if not database_exists:
                    connection.execution_options(isolation_level="AUTOCOMMIT").execute(
                        text(f"CREATE DATABASE {quote(connection, url.database)} OWNER {OWNER}")
                    )
                    connection.commit()
                    connection.execution_options(isolation_level="READ COMMITTED")
                with target.begin() as database:
                    database.execute(text("SET LOCAL lock_timeout = '30s'"))
                    database.execute(text("SET LOCAL statement_timeout = '120s'"))
                    # Adopt only the explicitly supported current-stage objects.
                    tables = database.execute(
                        text(
                            "SELECT n.nspname, c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=ANY(:schemas) AND c.relkind IN ('r','p','v','m','f')"
                        ),
                        {"schemas": list(SCHEMAS)},
                    ).all()
                    if any(f"{schema}.{name}" not in TABLES for schema, name in tables):
                        raise ConfigurationError("目标库含有当前迁移范围外的对象，请先核对数据库。")
                    functions = database.execute(
                        text(
                            "SELECT n.nspname, p.proname, p.pronargs, p.prokind, "
                            "p.prorettype = 'pg_catalog.trigger'::regtype "
                            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname=ANY(:schemas)"
                        ),
                        {"schemas": list(SCHEMAS)},
                    ).all()
                    if any(
                        f"{schema}.{name}" not in TRIGGER_FUNCTIONS
                        or nargs != 0
                        or kind != "f"
                        or not is_trigger
                        for schema, name, nargs, kind, is_trigger in functions
                    ):
                        raise ConfigurationError("目标库含有当前迁移范围外的函数，请先核对数据库。")
                    db = quote(database, url.database)
                    database.execute(text(f"ALTER DATABASE {db} OWNER TO {OWNER}"))
                    database.execute(
                        text(
                            f"REVOKE ALL ON DATABASE {db} FROM PUBLIC, {MIGRATOR}, {APP}, {BACKUP}"
                        )
                    )
                    database.execute(
                        text(f"GRANT CONNECT ON DATABASE {db} TO {MIGRATOR}, {APP}, {BACKUP}")
                    )
                    if secrets.get(CHECKPOINT):
                        database.execute(text(f"REVOKE ALL ON DATABASE {db} FROM {CHECKPOINT}"))
                        database.execute(text(f"GRANT CONNECT ON DATABASE {db} TO {CHECKPOINT}"))
                    database.execute(
                        text(f"REVOKE CREATE ON SCHEMA public FROM PUBLIC, {APP}, {BACKUP}")
                    )
                    database.execute(text("CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.6'"))
                    database.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                    for schema in SCHEMAS:
                        if database.scalar(
                            text("SELECT 1 FROM pg_namespace WHERE nspname=:schema"),
                            {"schema": schema},
                        ):
                            database.execute(text(f"ALTER SCHEMA {schema} OWNER TO {OWNER}"))
                    for schema, name in tables:
                        database.execute(
                            text(
                                f"ALTER TABLE {quote(database, schema)}.{quote(database, name)} "
                                f"OWNER TO {OWNER}"
                            )
                        )
                    for schema, name, *_ in functions:
                        qualified = f"{quote(database, schema)}.{quote(database, name)}"
                        database.execute(text(f"ALTER FUNCTION {qualified}() OWNER TO {OWNER}"))
                    business_tables = {
                        name for name in TABLES if not name.startswith("langgraph_checkpoint.")
                    }
                    if business_tables <= {f"{schema}.{name}" for schema, name in tables}:
                        grant_runtime(database)
            finally:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": PROVISION_LOCK})
                connection.commit()
    finally:
        target.dispose()
        maintenance.dispose()
