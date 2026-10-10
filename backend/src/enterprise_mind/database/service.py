"""Domain services for Database MCP configuration and schema refreshes."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.database_schemas import (
    DatabaseSourceCreate,
    DatabaseSourceOutput,
    DatabaseSourceUpdate,
    RelationPolicyOutput,
    RelationPolicyUpdate,
    SchemaCatalogOutput,
    SchemaColumnOutput,
    SchemaConstraintOutput,
    SchemaRefreshOutput,
    SchemaRelationOutput,
)
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.config import ConnectionOptions
from enterprise_mind.database.contracts import (
    SchemaCatalog,
    relation_signature,
)
from enterprise_mind.database.contracts import (
    SchemaColumn as ContractColumn,
)
from enterprise_mind.database.contracts import (
    SchemaConstraint as ContractConstraint,
)
from enterprise_mind.database.contracts import (
    SchemaRelation as ContractRelation,
)
from enterprise_mind.database.models import (
    DatabaseSource,
    RelationDepartmentGrant,
    RelationPolicy,
    SchemaColumn,
    SchemaConstraint,
    SchemaRelation,
    SchemaSnapshot,
)
from enterprise_mind.database.projection import sanitize_comment
from enterprise_mind.identity.management import ORGANIZATION_LOCK
from enterprise_mind.identity.models import Department
from enterprise_mind.identity.security import BIGINT_MAX, parse_id
from enterprise_mind.ingestion.models import OutboxEvent

SCHEMA_REFRESH_EVENT = "database.schema_refresh.requested"
SCHEMA_REFRESH_DEDUP_PREFIX = "database.schema_refresh:"
ALLOWED_SECRET_SCHEMES = {"secret", "secret-file", "vault", "k8s", "env"}
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class SchemaFetcher(Protocol):
    async def __call__(self, source: DatabaseSource) -> Any: ...


def _error(name: str, fallback: ErrorCode = ErrorCode.VALIDATION_ERROR) -> APIError:
    return APIError(getattr(ErrorCode, name, fallback))


def _check_row_version(value: int, expected: int) -> None:
    if value != expected or expected == BIGINT_MAX:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)


def _bump(entity: DatabaseSource | RelationPolicy) -> None:
    if entity.row_version == BIGINT_MAX:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    entity.row_version += 1
    entity.updated_at = datetime.now(UTC)


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise APIError(ErrorCode.NAME_CONFLICT) from None
        raise


def _validate_secret_ref(value: str) -> str:
    value = value.strip()
    if not value or any(char.isspace() for char in value) or "@" in value:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if "://" in value:
        scheme, reference = value.split("://", 1)
        if (
            scheme not in ALLOWED_SECRET_SCHEMES
            or not reference
            or ".." in reference
            or reference.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:", reference)
            or any(char in reference for char in "\x00\r\n?#")
        ):
            raise APIError(ErrorCode.VALIDATION_ERROR)
    elif (
        not re.fullmatch(r"[A-Za-z0-9._:-]{1,500}", value)
        or ".." in value
        or re.match(r"^[A-Za-z]:", value)
    ):
        # A deployment may expose an allowlisted Secret by name.  Bare local
        # paths, DSNs and password-bearing connection strings never pass this
        # branch.
        raise APIError(ErrorCode.VALIDATION_ERROR)
    return value


def _validate_host(value: str) -> str:
    value = value.strip()
    # A host is metadata, never a DSN or a local file/URL.  IPv6 colons remain
    # valid; credentials and path/URL syntax are not.
    if not value or "://" in value or "/" in value or "\\" in value or "@" in value:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    return value


def _validate_database_name(value: str) -> str:
    value = value.strip()
    if not value or "://" in value or "/" in value or "\\" in value or "@" in value:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    return value


def _bound_database_name(session: AsyncSession) -> str | None:
    """Read only the EnterpriseMind database name from the local bind."""

    try:
        bind = session.get_bind()
        url = getattr(bind, "url", None)
        return getattr(url, "database", None)
    except (AttributeError, RuntimeError):
        # Unit fakes and externally managed sessions may not expose a bind;
        # source isolation is still enforced by deployment/MCP networking.
        return None


def _reject_enterprise_database(session: AsyncSession, database_name: str) -> None:
    local_name = _bound_database_name(session)
    if local_name is not None and database_name == local_name:
        raise APIError(ErrorCode.VALIDATION_ERROR)


async def _lock_database_admin(session: AsyncSession) -> None:
    """Share organization writes' transaction advisory lock.

    API handlers already acquire this lock through ``admin_write``; PostgreSQL
    transaction advisory locks are re-entrant, so direct service callers use
    the same serialization boundary without a second lock domain.
    """

    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORGANIZATION_LOCK})


def _validate_connection_options(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        return ConnectionOptions.model_validate(value).model_dump(exclude_unset=True)
    except ValueError:
        raise APIError(ErrorCode.VALIDATION_ERROR) from None


def source_output(source: DatabaseSource) -> DatabaseSourceOutput:
    return DatabaseSourceOutput.model_validate(
        {
            "id": str(source.id),
            "name": source.name,
            "host": source.host,
            "port": source.port,
            "database_name": source.database_name,
            "ssl_mode": source.ssl_mode,
            "connection_options": source.connection_options,
            "status": source.status,
            "active_snapshot_id": (
                str(source.active_snapshot_id) if source.active_snapshot_id is not None else None
            ),
            "row_version": source.row_version,
            "configuration_version": source.configuration_version,
            "created_at": source.created_at,
            "updated_at": source.updated_at,
        }
    )


async def _require_complete_identity(session: AsyncSession, snapshot_id: int) -> None:
    incomplete_id = await session.scalar(
        select(SchemaRelation.id)
        .where(
            SchemaRelation.snapshot_id == snapshot_id,
            or_(
                SchemaRelation.relation_oid.is_(None),
                SchemaRelation.relation_signature.is_(None),
                (SchemaRelation.relation_type == "view")
                & SchemaRelation.view_definition_signature.is_(None),
            ),
        )
        .limit(1)
    )
    if incomplete_id is not None:
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.RESOURCE_NOT_FOUND)


def snapshot_output(snapshot: SchemaSnapshot) -> SchemaRefreshOutput:
    return SchemaRefreshOutput.model_validate(
        {
            "id": str(snapshot.id),
            "database_source_id": str(snapshot.database_source_id),
            "snapshot_no": snapshot.snapshot_no,
            "status": snapshot.status,
            "captured_at": snapshot.captured_at,
            "error_code": snapshot.error_code,
            "error_message": snapshot.error_message,
            "created_at": snapshot.created_at,
        }
    )


async def get_source(session: AsyncSession) -> DatabaseSource | None:
    return (
        await session.scalars(select(DatabaseSource).order_by(DatabaseSource.id).limit(1))
    ).one_or_none()


async def require_source(session: AsyncSession, *, active: bool = False) -> DatabaseSource:
    source = await get_source(session)
    if source is None or (active and source.status != "active"):
        raise _error("DATABASE_SOURCE_UNAVAILABLE", ErrorCode.RESOURCE_NOT_FOUND)
    return source


async def create_source(session: AsyncSession, body: DatabaseSourceCreate) -> DatabaseSourceOutput:
    await _lock_database_admin(session)
    if await get_source(session) is not None:
        raise _error("DATABASE_SOURCE_ALREADY_CONFIGURED", ErrorCode.NAME_CONFLICT)
    _validate_host(body.host)
    _validate_database_name(body.database_name)
    _reject_enterprise_database(session, body.database_name.strip())
    secret_ref = _validate_secret_ref(body.secret_ref)
    options = _validate_connection_options(body.connection_options)
    source = DatabaseSource(
        name=body.name.strip(),
        host=body.host.strip(),
        port=body.port,
        database_name=body.database_name.strip(),
        ssl_mode=body.ssl_mode.strip(),
        secret_ref=secret_ref,
        connection_options=options,
        status=body.status,
        configuration_version=1,
    )
    session.add(source)
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise _error("DATABASE_SOURCE_ALREADY_CONFIGURED", ErrorCode.NAME_CONFLICT) from None
        raise
    return source_output(source)


async def update_source(session: AsyncSession, body: DatabaseSourceUpdate) -> DatabaseSourceOutput:
    await _lock_database_admin(session)
    source = await require_source(session)
    source = (
        await session.scalars(
            select(DatabaseSource)
            .where(DatabaseSource.id == source.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()
    _check_row_version(source.row_version, body.row_version)
    values = body.model_dump(exclude={"row_version"}, exclude_unset=True)
    if "host" in values:
        values["host"] = _validate_host(cast(str, values["host"]))
    if "database_name" in values:
        values["database_name"] = _validate_database_name(cast(str, values["database_name"]))
        _reject_enterprise_database(session, cast(str, values["database_name"]).strip())
    if "secret_ref" in values:
        values["secret_ref"] = _validate_secret_ref(cast(str, values["secret_ref"]))
    if "connection_options" in values:
        values["connection_options"] = _validate_connection_options(
            cast(Mapping[str, object] | None, values["connection_options"])
        )
    changed = any(getattr(source, key) != value for key, value in values.items())
    if changed:
        connection_fields = {
            "host",
            "port",
            "database_name",
            "ssl_mode",
            "secret_ref",
            "connection_options",
        }
        connection_changed = any(
            key in connection_fields and getattr(source, key) != value
            for key, value in values.items()
        )
        runtime_changed = connection_changed or (
            "status" in values and source.status != values["status"]
        )
        for key, value in values.items():
            setattr(source, key, value.strip() if isinstance(value, str) else value)
        if runtime_changed:
            if source.configuration_version == BIGINT_MAX:
                raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
            source.configuration_version += 1
            await session.execute(
                update(SchemaSnapshot)
                .where(
                    SchemaSnapshot.database_source_id == source.id,
                    SchemaSnapshot.status == "building",
                )
                .values(
                    status="failed",
                    error_code="DATABASE_SOURCE_CHANGED",
                    error_message="Schema refresh invalidated by a source configuration change.",
                )
            )
        source_must_detach_snapshot = connection_changed or (
            "status" in values and source.status != "active"
        )
        if source_must_detach_snapshot:
            # A changed target/credential or a disabled/error source cannot
            # retain an active pointer.  Retire the old snapshot and clear the
            # pointer in the same transaction so the deferred source/snapshot
            # trigger sees a legal final state.  Policies remain as explicit
            # records and are revalidated by the next complete refresh.
            old_active_id = source.active_snapshot_id
            if old_active_id is not None:
                await session.execute(
                    update(SchemaSnapshot)
                    .where(SchemaSnapshot.id == old_active_id, SchemaSnapshot.status == "active")
                    .values(status="retired")
                )
                source.active_snapshot_id = None
        _bump(source)
        await _flush(session)
    return source_output(source)


async def _existing_refresh_for_key(
    session: AsyncSession, source_id: int, actor_user_id: int, key: str
) -> SchemaSnapshot | None:
    # Payload is a control envelope only; it contains no SQL, credentials or
    # external data.  The Python fallback keeps this helper portable across
    # SQLAlchemy/PostgreSQL JSON expression implementations.
    events = await session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.event_type == SCHEMA_REFRESH_EVENT,
            OutboxEvent.aggregate_type == "schema_snapshot",
        )
    )
    for event in events:
        payload = event.payload or {}
        if (
            payload.get("database_source_id") == source_id
            and payload.get("actor_user_id") == actor_user_id
            and payload.get("idempotency_key") == key
        ):
            return await session.get(SchemaSnapshot, event.aggregate_id)
    return None


async def request_schema_refresh(
    session: AsyncSession,
    *,
    actor_user_id: int | None = None,
    idempotency_key: str | None = None,
) -> SchemaRefreshOutput:
    await _lock_database_admin(session)
    source = await require_source(session, active=True)
    if idempotency_key is not None and (
        not _IDEMPOTENCY_RE.fullmatch(idempotency_key) or actor_user_id is None
    ):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if idempotency_key is not None:
        existing = await _existing_refresh_for_key(
            session, source.id, actor_user_id or 0, idempotency_key
        )
        if existing is not None:
            return snapshot_output(existing)
    building = await session.scalar(
        select(SchemaSnapshot)
        .where(
            SchemaSnapshot.database_source_id == source.id,
            SchemaSnapshot.status == "building",
        )
        .order_by(SchemaSnapshot.id.desc())
        .limit(1)
    )
    if building is not None:
        return snapshot_output(building)

    latest = await session.scalar(
        select(func.coalesce(func.max(SchemaSnapshot.snapshot_no), 0)).where(
            SchemaSnapshot.database_source_id == source.id
        )
    )
    snapshot = SchemaSnapshot(
        database_source_id=source.id,
        snapshot_no=int(latest or 0) + 1,
        status="building",
    )
    session.add(snapshot)
    await session.flush()
    payload: dict[str, object] = {
        "database_source_id": source.id,
        "schema_snapshot_id": snapshot.id,
    }
    if actor_user_id is not None:
        payload["actor_user_id"] = actor_user_id
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    session.add(
        OutboxEvent(
            aggregate_type="schema_snapshot",
            aggregate_id=snapshot.id,
            event_type=SCHEMA_REFRESH_EVENT,
            payload=payload,
            deduplication_key=f"{SCHEMA_REFRESH_DEDUP_PREFIX}{snapshot.id}",
        )
    )
    await session.flush()
    return snapshot_output(snapshot)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, Any], dumped)
    if hasattr(value, "__dict__"):
        return cast(Mapping[str, Any], vars(value))
    raise ValueError("schema item must be an object")


def _field(item: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return default


def _required_text(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {field}")
    return value


def _validate_normalized_relation(relation: ContractRelation) -> None:
    """Apply persistence bounds and catalog exclusions to every fetch shape."""

    if (
        len(relation.schema_name) > 200
        or len(relation.relation_name) > 200
        or relation.schema_name in {"pg_catalog", "information_schema"}
        or relation.schema_name == "pg_toast"
        or relation.schema_name.startswith(("pg_temp", "pg_toast"))
    ):
        raise ValueError("system or oversized schema relation is not allowed")
    if relation.relation_oid is not None and relation.relation_oid > 2**32 - 1:
        raise ValueError("relation oid is out of range")
    ordinals = [column.ordinal for column in relation.columns]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("relation column ordinals must be unique")
    for column in relation.columns:
        if len(column.name) > 200 or len(column.data_type) > 300:
            raise ValueError("oversized schema column is not allowed")
    for constraint in relation.constraints:
        if len(constraint.name) > 300:
            raise ValueError("oversized schema constraint is not allowed")
        if (
            constraint.referenced_schema_name is not None
            and len(constraint.referenced_schema_name) > 200
        ) or (
            constraint.referenced_relation_name is not None
            and len(constraint.referenced_relation_name) > 200
        ):
            raise ValueError("oversized foreign-key target is not allowed")


async def _stored_relation_signatures(
    session: AsyncSession, snapshot_id: int | None
) -> dict[tuple[str, str], str]:
    if snapshot_id is None:
        return {}
    relations = list(
        (
            await session.scalars(
                select(SchemaRelation)
                .where(SchemaRelation.snapshot_id == snapshot_id)
                .order_by(SchemaRelation.schema_name, SchemaRelation.relation_name)
            )
        ).all()
    )
    if not relations:
        return {}
    relation_ids = [relation.id for relation in relations]
    columns = list(
        (
            await session.scalars(
                select(SchemaColumn)
                .where(SchemaColumn.relation_id.in_(relation_ids))
                .order_by(SchemaColumn.relation_id, SchemaColumn.ordinal)
            )
        ).all()
    )
    constraints = list(
        (
            await session.scalars(
                select(SchemaConstraint)
                .where(SchemaConstraint.relation_id.in_(relation_ids))
                .order_by(SchemaConstraint.relation_id, SchemaConstraint.constraint_name)
            )
        ).all()
    )
    columns_by_relation: dict[int, list[SchemaColumn]] = {
        relation_id: [] for relation_id in relation_ids
    }
    constraints_by_relation: dict[int, list[SchemaConstraint]] = {
        relation_id: [] for relation_id in relation_ids
    }
    for column in columns:
        columns_by_relation[column.relation_id].append(column)
    for constraint in constraints:
        constraints_by_relation[constraint.relation_id].append(constraint)
    signatures: dict[tuple[str, str], str] = {}
    for relation in relations:
        if relation.relation_signature:
            signatures[(relation.schema_name, relation.relation_name)] = relation.relation_signature
            continue
        # 0010 snapshots have no identity/signature columns.  Construct a
        # deterministic legacy signature only for refresh comparison; a newly
        # captured PostgreSQL catalog includes the OID and view-definition
        # digest, so a legacy policy is conservatively invalidated rather than
        # inheriting authorization across an unproven object replacement.
        contract = ContractRelation(
            schema_name=relation.schema_name,
            relation_name=relation.relation_name,
            relation_type=relation.relation_type,
            columns=tuple(
                ContractColumn(
                    column.column_name,
                    column.data_type,
                    column.comment,
                    column.ordinal,
                    column.is_nullable,
                    False,
                )
                for column in columns_by_relation[relation.id]
            ),
            comment=relation.comment,
            constraints=tuple(
                ContractConstraint(
                    constraint.constraint_name,
                    constraint.constraint_type,
                    tuple(constraint.column_names),
                    constraint.referenced_schema_name,
                    constraint.referenced_relation_name,
                    tuple(constraint.referenced_column_names or ()),
                )
                for constraint in constraints_by_relation[relation.id]
            ),
            relation_oid=relation.relation_oid,
            view_definition_signature=relation.view_definition_signature,
        )
        signatures[(relation.schema_name, relation.relation_name)] = relation_signature(contract)
    return signatures


def _normalize_schema(raw: Any) -> tuple[ContractRelation, ...]:
    if isinstance(raw, SchemaCatalog):
        relations = raw.relations
    else:
        mapping = _as_mapping(raw)
        relations = _field(mapping, "relations", "schema_relations")
    if relations is None or isinstance(relations, str | bytes | bytearray):
        raise ValueError("schema relations are required")
    output: list[ContractRelation] = []
    seen_relations: set[tuple[str, str]] = set()
    for raw_relation in relations:
        relation = (
            raw_relation
            if isinstance(raw_relation, ContractRelation)
            else _as_mapping(raw_relation)
        )
        if isinstance(relation, ContractRelation):
            normalized = relation
        else:
            schema_name = _required_text(
                _field(relation, "schema_name", "schema"), field="schema_name", limit=200
            )
            relation_name = _required_text(
                _field(relation, "relation_name", "name"), field="relation_name", limit=200
            )
            relation_type = _field(relation, "relation_type", "type", default="table")
            comment = _field(relation, "comment", default=None)
            relation_oid = _field(relation, "relation_oid", "oid", default=None)
            if relation_oid is not None:
                relation_oid = int(relation_oid)
            view_definition_signature = _field(relation, "view_definition_signature", default=None)
            columns: list[ContractColumn] = []
            for index, raw_column in enumerate(
                _field(relation, "columns", default=()) or (), start=1
            ):
                column = _as_mapping(raw_column)
                columns.append(
                    ContractColumn(
                        name=_required_text(
                            _field(column, "name", "column_name"), field="column_name", limit=200
                        ),
                        data_type=_required_text(
                            _field(column, "data_type", "type"), field="data_type", limit=300
                        ),
                        comment=_field(column, "comment", default=None),
                        ordinal=int(_field(column, "ordinal", default=index)),
                        is_nullable=_field(column, "is_nullable", "nullable", default=None),
                        is_unique=bool(_field(column, "is_unique", "unique", default=False)),
                    )
                )
            constraints: list[ContractConstraint] = []
            for raw_constraint in _field(relation, "constraints", default=()) or ():
                constraint = _as_mapping(raw_constraint)
                constraint_type = _field(constraint, "constraint_type", "type")
                columns_value = _field(constraint, "column_names", "columns")
                refs_value = _field(
                    constraint,
                    "referenced_column_names",
                    "referenced_columns",
                    default=(),
                )
                constraints.append(
                    ContractConstraint(
                        name=_required_text(
                            _field(constraint, "name", "constraint_name"),
                            field="constraint_name",
                            limit=300,
                        ),
                        constraint_type=constraint_type,
                        column_names=tuple(columns_value or ()),
                        referenced_schema_name=_field(
                            constraint, "referenced_schema_name", "referenced_schema", default=None
                        ),
                        referenced_relation_name=_field(
                            constraint,
                            "referenced_relation_name",
                            "referenced_relation",
                            default=None,
                        ),
                        referenced_column_names=tuple(refs_value or ()),
                    )
                )
            normalized = ContractRelation(
                schema_name=schema_name,
                relation_name=relation_name,
                relation_type=relation_type,
                comment=comment,
                columns=tuple(columns),
                constraints=tuple(constraints),
                relation_oid=relation_oid,
                view_definition_signature=view_definition_signature,
            )
        if normalized.key in seen_relations:
            raise ValueError("duplicate schema relation")
        _validate_normalized_relation(normalized)
        seen_relations.add(normalized.key)
        output.append(normalized)
    relation_columns = {relation.key: set(relation.column_names) for relation in output}
    for relation in output:
        own_columns = relation_columns[relation.key]
        for constraint in relation.constraints:
            if not set(constraint.column_names).issubset(own_columns):
                raise ValueError("constraint references an unknown local column")
            if constraint.constraint_type == "foreign_key":
                assert constraint.referenced_schema_name is not None
                assert constraint.referenced_relation_name is not None
                assert constraint.referenced_column_names
                target_key = (
                    constraint.referenced_schema_name,
                    constraint.referenced_relation_name,
                )
                target_columns = relation_columns.get(target_key)
                if target_columns is None or not set(constraint.referenced_column_names).issubset(
                    target_columns
                ):
                    raise ValueError("foreign-key target is not present in the catalog")
    return tuple(output)


def _safe_failure(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, APIError):
        code = str(exc.code)
    else:
        code = "SCHEMA_REFRESH_FAILED"
    # Do not persist driver messages: they may contain host names, file paths,
    # DSNs, Secret references or data returned by the external database.
    return code[:100], "Schema refresh failed; the previous active snapshot remains in use."


async def _mark_failed(
    session: AsyncSession, snapshot_id: int, code: str, message: str
) -> SchemaSnapshot | None:
    snapshot = (
        await session.scalars(
            select(SchemaSnapshot)
            .where(SchemaSnapshot.id == snapshot_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if snapshot is None:
        return None
    if snapshot.status in {"active", "retired"}:
        return snapshot
    await session.execute(delete(SchemaRelation).where(SchemaRelation.snapshot_id == snapshot_id))
    snapshot.status = "failed"
    snapshot.error_code = code[:100]
    snapshot.error_message = message[:500]
    await session.flush()
    return snapshot


async def process_schema_refresh(
    session: AsyncSession,
    snapshot_id: int,
    fetch_schema: SchemaFetcher | Callable[[DatabaseSource], Awaitable[Any]],
) -> SchemaRefreshOutput:
    """Fetch, validate and atomically activate one building snapshot.

    The callable is the narrow MCP ``fetch_schema`` boundary.  It receives a
    persisted source row containing only non-secret connection metadata; the
    MCP adapter resolves ``secret_ref`` inside its own controlled runtime.
    """

    snapshot = (
        await session.scalars(
            select(SchemaSnapshot)
            .where(SchemaSnapshot.id == snapshot_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if snapshot is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    if snapshot.status != "building":
        return snapshot_output(snapshot)
    source = await session.get(DatabaseSource, snapshot.database_source_id)
    if source is None:
        failed = await _mark_failed(
            session,
            snapshot.id,
            "DATABASE_SOURCE_UNAVAILABLE",
            "Schema refresh failed; the previous active snapshot remains in use.",
        )
        assert failed is not None
        return snapshot_output(failed)
    # Display-name edits are intentionally excluded from this binding.  A
    # status or connection/credential change still invalidates a build.
    source_configuration_version = source.configuration_version
    source_status = source.status
    try:
        result = fetch_schema(source)
        if inspect.isawaitable(result):
            result = await result
        catalog = _normalize_schema(result)
    except Exception as exc:
        code, message = _safe_failure(exc)
        failed = await _mark_failed(session, snapshot.id, code, message)
        assert failed is not None
        return snapshot_output(failed)

    # Re-lock the source only after the network/MCP call.  A source update in
    # the meantime invalidates this build and cannot replace its old catalog.
    await _lock_database_admin(session)
    source = (
        await session.scalars(
            select(DatabaseSource)
            .where(DatabaseSource.id == snapshot.database_source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    snapshot = (
        await session.scalars(
            select(SchemaSnapshot)
            .where(SchemaSnapshot.id == snapshot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if source is None or snapshot is None or snapshot.status != "building":
        if snapshot is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return snapshot_output(snapshot)
    if (
        source.status != "active"
        or source.status != source_status
        or source.configuration_version != source_configuration_version
    ):
        failed = await _mark_failed(
            session,
            snapshot.id,
            "DATABASE_SOURCE_CHANGED",
            "Schema refresh failed; the previous active snapshot remains in use.",
        )
        assert failed is not None
        return snapshot_output(failed)
    current_active = None
    if source.active_snapshot_id is not None and source.active_snapshot_id != snapshot.id:
        current_active = await session.get(
            SchemaSnapshot, source.active_snapshot_id, populate_existing=True
        )
    if (
        current_active is not None
        and current_active.status == "active"
        and current_active.snapshot_no > snapshot.snapshot_no
    ):
        snapshot.status = "retired"
        snapshot.error_code = "SCHEMA_REFRESH_SUPERSEDED"
        snapshot.error_message = (
            "Schema refresh was superseded by a newer active snapshot; no catalog changes applied."
        )
        await session.flush()
        return snapshot_output(snapshot)

    old_signatures = await _stored_relation_signatures(session, source.active_snapshot_id)

    relation_rows: dict[tuple[str, str], SchemaRelation] = {}
    for relation in catalog:
        row = SchemaRelation(
            snapshot_id=snapshot.id,
            schema_name=relation.schema_name,
            relation_name=relation.relation_name,
            relation_type=relation.relation_type,
            comment=relation.comment,
            relation_oid=relation.relation_oid,
            relation_signature=relation_signature(relation),
            view_definition_signature=relation.view_definition_signature,
        )
        session.add(row)
        relation_rows[relation.key] = row
    await session.flush()
    for relation in catalog:
        relation_row = relation_rows[relation.key]
        for column in relation.columns:
            session.add(
                SchemaColumn(
                    relation_id=relation_row.id,
                    ordinal=column.ordinal,
                    column_name=column.name,
                    data_type=column.data_type,
                    comment=column.comment,
                    is_nullable=column.is_nullable,
                )
            )
        for constraint in relation.constraints:
            session.add(
                SchemaConstraint(
                    relation_id=relation_row.id,
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    column_names=list(constraint.column_names),
                    referenced_schema_name=constraint.referenced_schema_name,
                    referenced_relation_name=constraint.referenced_relation_name,
                    referenced_column_names=list(constraint.referenced_column_names)
                    if constraint.referenced_column_names
                    else None,
                )
            )
    await session.flush()

    new_signatures = {relation.key: relation_signature(relation) for relation in catalog}
    policies = list(
        (
            await session.scalars(
                select(RelationPolicy)
                .where(RelationPolicy.database_source_id == source.id)
                .with_for_update()
            )
        ).all()
    )
    invalid_policy_ids = [
        policy.id
        for policy in policies
        if old_signatures.get((policy.schema_name, policy.relation_name))
        != new_signatures.get((policy.schema_name, policy.relation_name))
    ]
    for policy in policies:
        if policy.id in invalid_policy_ids and policy.enabled:
            policy.enabled = False
            _bump(policy)
    if invalid_policy_ids:
        await session.execute(
            delete(RelationDepartmentGrant).where(
                RelationDepartmentGrant.relation_policy_id.in_(invalid_policy_ids)
            )
        )
    await session.flush()

    now = datetime.now(UTC)
    snapshot.status = "ready"
    snapshot.captured_at = now
    snapshot.error_code = None
    snapshot.error_message = None
    await session.flush()
    old_id = source.active_snapshot_id
    if old_id is not None and old_id != snapshot.id:
        await session.execute(
            update(SchemaSnapshot)
            .where(SchemaSnapshot.id == old_id, SchemaSnapshot.status == "active")
            .values(status="retired")
        )
    snapshot.status = "active"
    # The database trigger validates the pointer against an already-active
    # snapshot.  Flush the snapshot transition before changing the source FK;
    # this also keeps the two states observable only inside this transaction.
    await session.flush()
    source.active_snapshot_id = snapshot.id
    await session.flush()
    return snapshot_output(snapshot)


async def process_schema_refresh_outbox(
    session: AsyncSession,
    fetch_schema: SchemaFetcher | Callable[[DatabaseSource], Awaitable[Any]],
    *,
    event_id: int | None = None,
) -> SchemaRefreshOutput | None:
    """Process one dedicated schema-refresh Outbox event idempotently."""

    now = datetime.now(UTC)
    query = select(OutboxEvent).where(OutboxEvent.event_type == SCHEMA_REFRESH_EVENT)
    if event_id is not None:
        query = query.where(OutboxEvent.id == event_id)
    else:
        query = query.where(
            or_(
                OutboxEvent.status.in_(["pending", "failed"]),
                (OutboxEvent.status == "publishing")
                & or_(
                    OutboxEvent.lease_expires_at.is_(None),
                    OutboxEvent.lease_expires_at <= now,
                ),
            )
        )
    event = (
        await session.scalars(
            query.order_by(OutboxEvent.id).limit(1).with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if event is None:
        return None
    if event.status == "published":
        snapshot = await session.get(SchemaSnapshot, event.aggregate_id)
        return snapshot_output(snapshot) if snapshot else None
    event.status = "publishing"
    event.attempt_count += 1
    event.lease_owner = "schema-refresh"
    event.lease_expires_at = now + timedelta(seconds=120)
    await session.flush()
    result = await process_schema_refresh(session, event.aggregate_id, fetch_schema)
    event.status = "published"
    event.published_at = datetime.now(UTC)
    event.lease_owner = None
    event.lease_expires_at = None
    event.last_error = None
    await session.flush()
    return result


async def poll_schema_refresh_outbox(
    session: AsyncSession,
    fetch_schema: SchemaFetcher | Callable[[DatabaseSource], Awaitable[Any]],
    *,
    limit: int = 10,
) -> int:
    if limit < 1 or limit > 100:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    count = 0
    for _ in range(limit):
        result = await process_schema_refresh_outbox(session, fetch_schema)
        if result is None:
            break
        count += 1
    return count


async def schema_refresh(
    session: AsyncSession,
    snapshot_id: int,
    fetch_schema: SchemaFetcher | Callable[[DatabaseSource], Awaitable[Any]],
) -> SchemaRefreshOutput:
    """Compatibility alias for a dedicated worker adapter."""

    return await process_schema_refresh(session, snapshot_id, fetch_schema)


async def get_refresh(session: AsyncSession, snapshot_id: int) -> SchemaRefreshOutput:
    snapshot = await session.get(SchemaSnapshot, snapshot_id)
    if snapshot is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return snapshot_output(snapshot)


async def get_latest_refresh(session: AsyncSession) -> SchemaRefreshOutput | None:
    """Return durable building, active, failed or retired refresh state."""

    source = await get_source(session)
    if source is None:
        return None
    snapshot = await session.scalar(
        select(SchemaSnapshot)
        .where(SchemaSnapshot.database_source_id == source.id)
        .order_by(SchemaSnapshot.snapshot_no.desc(), SchemaSnapshot.id.desc())
        .limit(1)
    )
    return snapshot_output(snapshot) if snapshot is not None else None


async def _active_snapshot(session: AsyncSession) -> tuple[DatabaseSource, SchemaSnapshot]:
    source = await require_source(session, active=True)
    if source.active_snapshot_id is None:
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.RESOURCE_NOT_FOUND)
    snapshot = await session.get(SchemaSnapshot, source.active_snapshot_id)
    if snapshot is None or snapshot.status != "active":
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.RESOURCE_NOT_FOUND)
    await _require_complete_identity(session, snapshot.id)
    return source, snapshot


async def _all_relations(
    session: AsyncSession, snapshot_id: int
) -> tuple[list[SchemaRelation], dict[int, list[SchemaColumn]], dict[int, list[SchemaConstraint]]]:
    relations = list(
        (
            await session.scalars(
                select(SchemaRelation)
                .where(SchemaRelation.snapshot_id == snapshot_id)
                .order_by(SchemaRelation.schema_name, SchemaRelation.relation_name)
            )
        ).all()
    )
    ids = [row.id for row in relations]
    columns: dict[int, list[SchemaColumn]] = {row_id: [] for row_id in ids}
    constraints: dict[int, list[SchemaConstraint]] = {row_id: [] for row_id in ids}
    if ids:
        for row in (
            await session.scalars(
                select(SchemaColumn)
                .where(SchemaColumn.relation_id.in_(ids))
                .order_by(SchemaColumn.relation_id, SchemaColumn.ordinal)
            )
        ).all():
            columns[row.relation_id].append(row)
        for constraint_row in (
            await session.scalars(
                select(SchemaConstraint)
                .where(SchemaConstraint.relation_id.in_(ids))
                .order_by(SchemaConstraint.relation_id, SchemaConstraint.id)
            )
        ).all():
            constraints[constraint_row.relation_id].append(constraint_row)
    return relations, columns, constraints


def _relation_output(
    relation: SchemaRelation,
    columns: Sequence[SchemaColumn],
    constraints: Sequence[SchemaConstraint],
) -> SchemaRelationOutput:
    unique_columns = {
        constraint.column_names[0]
        for constraint in constraints
        if constraint.constraint_type in {"primary_key", "unique"}
        and len(constraint.column_names) == 1
        and (
            constraint.constraint_type == "primary_key"
            or next(
                (
                    column.is_nullable is False
                    for column in columns
                    if column.column_name == constraint.column_names[0]
                ),
                False,
            )
        )
    }
    return SchemaRelationOutput.model_validate(
        {
            "id": str(relation.id),
            "schema_name": relation.schema_name,
            "relation_name": relation.relation_name,
            "relation_type": relation.relation_type,
            "comment": sanitize_comment(relation.comment),
            "columns": [
                SchemaColumnOutput.model_validate(
                    {
                        "id": str(column.id),
                        "ordinal": column.ordinal,
                        "column_name": column.column_name,
                        "data_type": column.data_type,
                        "comment": sanitize_comment(column.comment),
                        "is_nullable": column.is_nullable,
                        "is_unique": column.column_name in unique_columns,
                    }
                )
                for column in columns
            ],
            "constraints": [
                SchemaConstraintOutput.model_validate(
                    {
                        "id": str(constraint.id),
                        "constraint_name": constraint.constraint_name,
                        "constraint_type": constraint.constraint_type,
                        "column_names": list(constraint.column_names),
                        "referenced_schema_name": constraint.referenced_schema_name,
                        "referenced_relation_name": constraint.referenced_relation_name,
                        "referenced_column_names": (
                            list(constraint.referenced_column_names)
                            if constraint.referenced_column_names is not None
                            else None
                        ),
                    }
                )
                for constraint in constraints
            ],
        }
    )


async def active_catalog(session: AsyncSession) -> SchemaCatalogOutput:
    source, snapshot = await _active_snapshot(session)
    relations, columns, constraints = await _all_relations(session, snapshot.id)
    return SchemaCatalogOutput.model_validate(
        {
            "database_source_id": str(source.id),
            "schema_snapshot_id": str(snapshot.id),
            "snapshot_no": snapshot.snapshot_no,
            "relations": [
                _relation_output(row, columns[row.id], constraints[row.id]) for row in relations
            ],
        }
    )


async def _policy_output(
    session: AsyncSession,
    source: DatabaseSource,
    policy: RelationPolicy,
    relation_type: str | None,
) -> RelationPolicyOutput:
    department_ids = list(
        await session.scalars(
            select(RelationDepartmentGrant.department_id)
            .where(RelationDepartmentGrant.relation_policy_id == policy.id)
            .order_by(RelationDepartmentGrant.department_id)
        )
    )
    return RelationPolicyOutput.model_validate(
        {
            "id": str(policy.id),
            "database_source_id": str(source.id),
            "schema_name": policy.schema_name,
            "relation_name": policy.relation_name,
            "relation_type": relation_type,
            "enabled": policy.enabled,
            "department_ids": [str(department_id) for department_id in department_ids],
            "row_version": policy.row_version,
            "created_at": policy.created_at,
            "updated_at": policy.updated_at,
        }
    )


async def list_relation_policies(session: AsyncSession) -> list[RelationPolicyOutput]:
    source, snapshot = await _active_snapshot(session)
    rows = list(
        (
            await session.execute(
                select(SchemaRelation, RelationPolicy)
                .outerjoin(
                    RelationPolicy,
                    (RelationPolicy.database_source_id == source.id)
                    & (RelationPolicy.schema_name == SchemaRelation.schema_name)
                    & (RelationPolicy.relation_name == SchemaRelation.relation_name),
                )
                .where(SchemaRelation.snapshot_id == snapshot.id)
                .order_by(SchemaRelation.schema_name, SchemaRelation.relation_name)
            )
        ).all()
    )
    output: list[RelationPolicyOutput] = []
    for relation, policy in rows:
        if policy is None:
            output.append(
                RelationPolicyOutput.model_validate(
                    {
                        "id": "0",
                        "database_source_id": str(source.id),
                        "schema_name": relation.schema_name,
                        "relation_name": relation.relation_name,
                        "relation_type": relation.relation_type,
                        "enabled": False,
                        "department_ids": [],
                        "row_version": 0,
                        "created_at": relation.created_at,
                        "updated_at": relation.created_at,
                    }
                )
            )
        else:
            output.append(await _policy_output(session, source, policy, relation.relation_type))
    return output


async def upsert_relation_policy(
    session: AsyncSession, body: RelationPolicyUpdate
) -> RelationPolicyOutput:
    await _lock_database_admin(session)
    source, snapshot = await _active_snapshot(session)
    relation = (
        await session.scalars(
            select(SchemaRelation).where(
                SchemaRelation.snapshot_id == snapshot.id,
                SchemaRelation.schema_name == body.schema_name,
                SchemaRelation.relation_name == body.relation_name,
            )
        )
    ).one_or_none()
    if relation is None:
        raise _error("DATABASE_RELATION_FORBIDDEN", ErrorCode.RESOURCE_NOT_FOUND)
    policy = (
        await session.scalars(
            select(RelationPolicy)
            .where(
                RelationPolicy.database_source_id == source.id,
                RelationPolicy.schema_name == body.schema_name,
                RelationPolicy.relation_name == body.relation_name,
            )
            .with_for_update()
        )
    ).one_or_none()
    if policy is None:
        if body.row_version is not None:
            raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
        policy = RelationPolicy(
            database_source_id=source.id,
            schema_name=body.schema_name,
            relation_name=body.relation_name,
            enabled=body.enabled,
        )
        session.add(policy)
        await session.flush()
    else:
        if body.row_version is None:
            raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
        _check_row_version(policy.row_version, body.row_version)
        policy.enabled = body.enabled
        _bump(policy)
    department_ids = [parse_id(value) for value in body.department_ids] if body.enabled else []
    if department_ids:
        existing_ids = set(
            await session.scalars(
                select(Department.id).where(
                    Department.id.in_(department_ids), Department.status == "active"
                )
            )
        )
        if existing_ids != set(department_ids):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    await session.execute(
        delete(RelationDepartmentGrant).where(
            RelationDepartmentGrant.relation_policy_id == policy.id
        )
    )
    session.add_all(
        [
            RelationDepartmentGrant(relation_policy_id=policy.id, department_id=department_id)
            for department_id in department_ids
        ]
    )
    await _flush(session)
    return await _policy_output(session, source, policy, relation.relation_type)


__all__ = [
    "SCHEMA_REFRESH_EVENT",
    "SchemaFetcher",
    "active_catalog",
    "create_source",
    "get_refresh",
    "get_latest_refresh",
    "get_source",
    "list_relation_policies",
    "poll_schema_refresh_outbox",
    "process_schema_refresh",
    "process_schema_refresh_outbox",
    "request_schema_refresh",
    "require_source",
    "schema_refresh",
    "snapshot_output",
    "source_output",
    "update_source",
    "upsert_relation_policy",
]
