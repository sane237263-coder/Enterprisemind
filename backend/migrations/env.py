"""Migrations run only with the connection owned by the deployment command."""

from alembic import context

from enterprise_mind.agent import generation_models  # noqa: F401
from enterprise_mind.database import export_models  # noqa: F401
from enterprise_mind.identity.models import Base
from enterprise_mind.memory import models as memory_models  # noqa: F401

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use enterprise-mind migrate; direct Alembic execution is disabled.")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    version_table_schema="infra",
    include_schemas=True,
)
with context.begin_transaction():
    context.run_migrations()
