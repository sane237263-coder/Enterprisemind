from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from enterprise_mind.identity.models import User
from enterprise_mind.identity.security import Passwords, normalize_name
from enterprise_mind.infrastructure.database import check_connection
from enterprise_mind.infrastructure.provision import MIGRATOR, OWNER, grant_runtime
from enterprise_mind.infrastructure.settings import (
    ApplicationConfig,
    BootstrapSettings,
    ConfigurationError,
    read_secret,
)

MIGRATION_LOCK = 4617078203315146317


def bootstrap_admin(session: Session, config: ApplicationConfig) -> bool:
    # Also excludes ordinary INSERTs while checking the empty-system boundary.
    session.execute(text("LOCK TABLE iam.users IN EXCLUSIVE MODE"))
    if session.scalar(select(func.count()).select_from(User)):
        if session.scalar(select(User.id).where(User.role == "admin").limit(1)) is None:
            raise ConfigurationError("用户表非空但没有管理员，需要受控恢复。")
        return False

    settings = BootstrapSettings()
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password_file:
        raise ConfigurationError("空系统必须提供首个管理员用户名和专用密码 Secret。")
    try:
        username = settings.bootstrap_admin_username.strip()
        username_key = normalize_name(username, max_length=100)
        if len(username) > 100:
            raise ValueError("invalid username length")
        password_hash = Passwords(config.auth).hash(
            read_secret(settings.bootstrap_admin_password_file)
        )
    except ValueError:
        raise ConfigurationError("首个管理员用户名或密码不符合策略。") from None
    session.add(
        User(
            username=username,
            username_key=username_key,
            password_hash=password_hash,
            role="admin",
            department_id=None,
            status="active",
        )
    )
    session.flush()
    return True


def migrate(url: URL, config: ApplicationConfig, backend_root: Path) -> bool:
    engine = create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": config.postgres.connection_timeout_seconds},
    )
    try:
        with engine.begin() as connection:
            if url.username == MIGRATOR:
                connection.execute(text(f"SET LOCAL ROLE {OWNER}"))
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(text("SET LOCAL statement_timeout = '120s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS infra"))
            alembic = Config(str(backend_root / "alembic.ini"))
            alembic.attributes["connection"] = connection
            command.upgrade(alembic, "head")
            if url.username == MIGRATOR:
                grant_runtime(connection)
            with Session(bind=connection, join_transaction_mode="rollback_only") as session:
                created = bootstrap_admin(session, config)
                session.flush()
            check_connection(connection)
            return created
    finally:
        engine.dispose()
