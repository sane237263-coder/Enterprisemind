"""Real-time authorization projection for Database MCP schema use.

The projection is request-local.  It never expands from the external database
and it does not cache policy or identity rows; callers may keep the returned
binding for one query invocation and revalidate it immediately before and
after execution.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.database.contracts import (
    SchemaCatalog,
)
from enterprise_mind.database.contracts import (
    SchemaColumn as CatalogColumn,
)
from enterprise_mind.database.contracts import (
    SchemaConstraint as CatalogConstraint,
)
from enterprise_mind.database.contracts import (
    SchemaRelation as CatalogRelation,
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
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity

MAX_COMMENT_CHARS = 1_000


def _error(name: str, fallback: ErrorCode = ErrorCode.PERMISSION_DENIED) -> APIError:
    # The stage-8 error values are registered by the application integration
    # branch.  The fallback keeps this module importable in an older checkout.
    return APIError(getattr(ErrorCode, name, fallback))


def sanitize_comment(value: str | None, *, limit: int = MAX_COMMENT_CHARS) -> str | None:
    """Return untrusted database metadata in a bounded, control-free form."""

    if value is None:
        return None
    clean: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("C"):
            # Preserve ordinary tab/newline layout as a single space.  All
            # other control and formatting characters are removed as well.
            clean.append(" ")
        else:
            clean.append(char)
    text = " ".join("".join(clean).split())
    return text[:limit] or None


@dataclass(frozen=True)
class BoundSchema:
    """Fixed source/snapshot/catalog binding for one query invocation."""

    database_source_id: int
    source_name: str
    database_name: str
    schema_snapshot_id: int
    snapshot_no: int
    source_version: int
    catalog: SchemaCatalog
    permission_fingerprint: str

    @property
    def source_id(self) -> int:
        return self.database_source_id

    @property
    def snapshot_id(self) -> int:
        return self.schema_snapshot_id

    @property
    def permission_digest(self) -> str:
        """Compatibility name used by the query persistence projection."""

        return self.permission_fingerprint

    @property
    def relation_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (relation.schema_name, relation.relation_name) for relation in self.catalog.relations
        )


# The descriptive name is useful to callers that do not depend on the query
# package's ``BoundSchema`` terminology.
SchemaBinding = BoundSchema


def _fingerprint(
    *,
    source_id: int,
    snapshot_id: int,
    role: str,
    department_id: int | None,
    user_id: int,
    user_version: int,
    department_state: tuple[int, str, int] | None,
    keys: set[tuple[str, str]],
    policy_state: Sequence[tuple[int, int, bool, tuple[tuple[int, int | None], ...]]],
) -> str:
    payload = {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "role": role,
        "department_id": department_id,
        "user_id": user_id,
        "user_version": user_version,
        "department_state": list(department_state) if department_state else None,
        "relations": sorted([list(key) for key in keys]),
        "policies": [
            [policy_id, row_version, enabled, list(department_ids)]
            for policy_id, row_version, enabled, department_ids in sorted(policy_state)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


async def _live_identity(session: AsyncSession, identity: CurrentIdentity) -> CurrentIdentity:
    """Reload user and department so status/department changes are immediate."""

    user = (
        await session.scalars(
            select(User)
            .where(User.id == identity.user.id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if user is None:
        raise APIError(ErrorCode.AUTH_REQUIRED)
    if (
        identity.authenticated_credential_version is not None
        and user.credential_version != identity.authenticated_credential_version
    ):
        raise APIError(ErrorCode.AUTH_REQUIRED)
    department = (
        await session.get(Department, user.department_id, populate_existing=True)
        if user.department_id is not None
        else None
    )
    current = CurrentIdentity(user, department, identity.authenticated_credential_version)
    AuthorizationService.require_active(current)
    return current


async def _source_and_snapshot(
    session: AsyncSession, snapshot_id: int | None
) -> tuple[DatabaseSource, SchemaSnapshot]:
    source = (
        await session.scalars(
            select(DatabaseSource)
            .where(DatabaseSource.status == "active")
            .order_by(DatabaseSource.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if source is None:
        raise _error("DATABASE_SOURCE_UNAVAILABLE", ErrorCode.PERMISSION_DENIED)

    selected_id = snapshot_id if snapshot_id is not None else source.active_snapshot_id
    if selected_id is None:
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.PERMISSION_DENIED)

    snapshot = (
        await session.scalars(
            select(SchemaSnapshot)
            .where(
                SchemaSnapshot.id == selected_id,
                SchemaSnapshot.database_source_id == source.id,
                # A fixed in-flight invocation may finish on the retired
                # snapshot after another complete snapshot becomes active.
                SchemaSnapshot.status.in_(["active", "retired"]),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if snapshot is None:
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.PERMISSION_DENIED)
    if snapshot_id is None and snapshot.status != "active":
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.PERMISSION_DENIED)
    incomplete_identity = await session.scalar(
        select(
            exists().where(
                SchemaRelation.snapshot_id == snapshot.id,
                or_(
                    SchemaRelation.relation_oid.is_(None),
                    SchemaRelation.relation_signature.is_(None),
                    and_(
                        SchemaRelation.relation_type == "view",
                        SchemaRelation.view_definition_signature.is_(None),
                    ),
                ),
            )
        )
    )
    if incomplete_identity:
        # Nullable columns keep the 0010 -> 0011 physical migration safe, but
        # they are never sufficient proof for a query authorization binding.
        raise _error("SCHEMA_SNAPSHOT_UNAVAILABLE", ErrorCode.PERMISSION_DENIED)
    return source, snapshot


async def _allowed_rows(
    session: AsyncSession,
    identity: CurrentIdentity,
    source: DatabaseSource,
    snapshot: SchemaSnapshot,
) -> list[tuple[SchemaRelation, RelationPolicy]]:
    grant = exists(
        select(RelationDepartmentGrant.id).where(
            RelationDepartmentGrant.relation_policy_id == RelationPolicy.id,
            RelationDepartmentGrant.department_id == identity.user.department_id,
        )
    )
    query = (
        select(SchemaRelation, RelationPolicy)
        .join(
            RelationPolicy,
            and_(
                RelationPolicy.database_source_id == source.id,
                RelationPolicy.schema_name == SchemaRelation.schema_name,
                RelationPolicy.relation_name == SchemaRelation.relation_name,
            ),
        )
        .where(
            SchemaRelation.snapshot_id == snapshot.id,
            RelationPolicy.enabled.is_(True),
        )
        .order_by(SchemaRelation.schema_name, SchemaRelation.relation_name)
    )
    if identity.user.role != "admin":
        query = query.where(grant)
    return [(relation, policy) for relation, policy in (await session.execute(query)).all()]


async def authorized_schema(
    session: AsyncSession,
    identity: CurrentIdentity,
    snapshot_id: int | None = None,
) -> SchemaBinding:
    """Build a current authorization intersection and bind one snapshot.

    ``snapshot_id`` is only used for a binding that has already been fixed by a
    query invocation.  It permits the snapshot to become ``retired`` during a
    refresh while still requiring current source/user/policy authorization.
    """

    current = await _live_identity(session, identity)
    source, snapshot = await _source_and_snapshot(session, snapshot_id)
    rows = await _allowed_rows(session, current, source, snapshot)
    allowed_keys = {(relation.schema_name, relation.relation_name) for relation, _ in rows}
    policy_state_rows = (
        await session.execute(
            select(
                RelationPolicy.id,
                RelationPolicy.row_version,
                RelationPolicy.enabled,
                RelationDepartmentGrant.id,
                RelationDepartmentGrant.department_id,
            )
            .outerjoin(
                RelationDepartmentGrant,
                RelationDepartmentGrant.relation_policy_id == RelationPolicy.id,
            )
            .where(RelationPolicy.database_source_id == source.id)
            .order_by(
                RelationPolicy.id,
                RelationDepartmentGrant.department_id,
            )
        )
    ).all()
    policy_state_map: dict[int, tuple[int, bool, list[tuple[int, int]]]] = {}
    for policy_id, row_version, enabled, grant_id, department_id in policy_state_rows:
        state = policy_state_map.setdefault(policy_id, (row_version, enabled, []))
        if department_id is not None:
            state[2].append((department_id, grant_id))
    policy_state = tuple(
        (policy_id, row_version, enabled, tuple(department_ids))
        for policy_id, (row_version, enabled, department_ids) in policy_state_map.items()
    )
    relation_ids = [relation.id for relation, _ in rows]
    columns: dict[int, list[SchemaColumn]] = {relation_id: [] for relation_id in relation_ids}
    constraints: dict[int, list[SchemaConstraint]] = {
        relation_id: [] for relation_id in relation_ids
    }
    if relation_ids:
        column_rows = await session.scalars(
            select(SchemaColumn)
            .where(SchemaColumn.relation_id.in_(relation_ids))
            .order_by(SchemaColumn.relation_id, SchemaColumn.ordinal)
        )
        for column in column_rows:
            columns[column.relation_id].append(column)
        constraint_rows = await session.scalars(
            select(SchemaConstraint)
            .where(SchemaConstraint.relation_id.in_(relation_ids))
            .order_by(SchemaConstraint.relation_id, SchemaConstraint.id)
        )
        for constraint in constraint_rows:
            constraints[constraint.relation_id].append(constraint)

    catalog_relations: list[CatalogRelation] = []
    for relation, _policy in rows:
        unique_columns = {
            constraint.column_names[0]
            for constraint in constraints[relation.id]
            if constraint.constraint_type in {"primary_key", "unique"}
            and len(constraint.column_names) == 1
            and (
                constraint.constraint_type == "primary_key"
                or next(
                    (
                        column.is_nullable is False
                        for column in columns[relation.id]
                        if column.column_name == constraint.column_names[0]
                    ),
                    False,
                )
            )
        }
        catalog_constraints: list[CatalogConstraint] = []
        for constraint in constraints[relation.id]:
            if constraint.constraint_type == "foreign_key":
                target_key = (
                    constraint.referenced_schema_name,
                    constraint.referenced_relation_name,
                )
                # A foreign key never grants access to its target.  Do not put
                # target names into the request-local catalog unless the target
                # is independently policy-authorized.
                if target_key not in allowed_keys:
                    continue
                catalog_constraints.append(
                    CatalogConstraint(
                        name=constraint.constraint_name,
                        constraint_type="foreign_key",
                        column_names=tuple(constraint.column_names),
                        referenced_schema_name=constraint.referenced_schema_name,
                        referenced_relation_name=constraint.referenced_relation_name,
                        referenced_column_names=tuple(constraint.referenced_column_names or ()),
                    )
                )
            else:
                catalog_constraints.append(
                    CatalogConstraint(
                        name=constraint.constraint_name,
                        constraint_type=constraint.constraint_type,
                        column_names=tuple(constraint.column_names),
                    )
                )
        catalog_relations.append(
            CatalogRelation(
                schema_name=relation.schema_name,
                relation_name=relation.relation_name,
                relation_type=relation.relation_type,
                relation_oid=relation.relation_oid,
                relation_signature=relation.relation_signature,
                view_definition_signature=relation.view_definition_signature,
                comment=sanitize_comment(relation.comment),
                columns=tuple(
                    CatalogColumn(
                        name=column.column_name,
                        data_type=column.data_type,
                        comment=sanitize_comment(column.comment),
                        ordinal=column.ordinal,
                        is_nullable=column.is_nullable,
                        is_unique=column.column_name in unique_columns,
                    )
                    for column in columns[relation.id]
                ),
                constraints=tuple(catalog_constraints),
            )
        )

    catalog = SchemaCatalog(tuple(catalog_relations))
    return BoundSchema(
        database_source_id=source.id,
        source_name=source.name,
        database_name=source.database_name,
        schema_snapshot_id=snapshot.id,
        snapshot_no=snapshot.snapshot_no,
        source_version=source.configuration_version,
        catalog=catalog,
        permission_fingerprint=_fingerprint(
            source_id=source.id,
            snapshot_id=snapshot.id,
            role=current.user.role,
            department_id=current.user.department_id,
            user_id=current.user.id,
            user_version=current.user.row_version,
            department_state=(
                (current.department.id, current.department.status, current.department.row_version)
                if current.department is not None
                else None
            ),
            keys=allowed_keys,
            policy_state=policy_state,
        ),
    )


async def reauthorize_schema(
    session: AsyncSession,
    identity: CurrentIdentity,
    binding: SchemaBinding,
) -> SchemaBinding:
    """Recompute permission for an existing binding before/after execution."""

    current = await authorized_schema(session, identity, snapshot_id=binding.snapshot_id)
    if (
        current.source_id != binding.source_id
        or current.snapshot_id != binding.snapshot_id
        or current.source_version != binding.source_version
        or current.permission_fingerprint != binding.permission_fingerprint
    ):
        raise _error("DATABASE_RELATION_FORBIDDEN")
    if not binding.relation_keys.issubset(current.relation_keys):
        # No relation names are included in the error details.  This makes a
        # revoked object indistinguishable from an otherwise forbidden object.
        raise _error("DATABASE_RELATION_FORBIDDEN")
    return current


async def revalidate_schema(
    session: AsyncSession,
    identity: CurrentIdentity,
    binding: BoundSchema,
    referenced_relations: tuple[str, ...],
) -> None:
    """Recheck only the relations a guarded query actually references.

    The fixed snapshot may be retired by a successful refresh.  Source
    configuration changes still invalidate the binding through the runtime
    configuration epoch and permission fingerprint.
    """

    current = await authorized_schema(session, identity, snapshot_id=binding.snapshot_id)
    if (
        current.source_id != binding.source_id
        or current.snapshot_id != binding.snapshot_id
        or current.source_version != binding.source_version
        or current.permission_fingerprint != binding.permission_fingerprint
    ):
        raise _error("DATABASE_RELATION_FORBIDDEN")
    # The model saw the whole first-stage catalog.  Any shrink of that
    # authorized set invalidates the invocation, even if the SQL happened not
    # to reference the revoked relation.
    if not binding.relation_keys.issubset(current.relation_keys):
        raise _error("DATABASE_RELATION_FORBIDDEN")
    # The SQL was generated from the original fixed catalog.  A grant added
    # after binding must not expand that catalog during this invocation; only
    # the current authorization check may invalidate it when access shrinks.
    identifiers = {relation.quoted_qualified_name for relation in binding.catalog.relations}
    for qualified in referenced_relations:
        if not isinstance(qualified, str) or qualified not in identifiers:
            raise _error("DATABASE_RELATION_FORBIDDEN")


def catalog_projection(binding: BoundSchema) -> list[dict[str, Any]]:
    """First projection stage: names/types/comments only."""

    return [
        {
            "schema_name": relation.schema_name,
            "relation_name": relation.relation_name,
            "relation_type": relation.relation_type,
            "comment": relation.comment,
        }
        for relation in binding.catalog.relations
    ]


def detail_projection(
    binding: BoundSchema, keys: Sequence[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Second projection stage, restricted to the already-authorized catalog."""

    requested = set(keys)
    return [
        {
            "schema_name": relation.schema_name,
            "relation_name": relation.relation_name,
            "relation_type": relation.relation_type,
            "comment": relation.comment,
            "columns": [
                {
                    "name": column.name,
                    "data_type": column.data_type,
                    "comment": column.comment,
                    "ordinal": column.ordinal,
                    "is_nullable": column.is_nullable,
                    "is_unique": column.is_unique,
                }
                for column in relation.columns
            ],
            "constraints": [
                {
                    "name": constraint.name,
                    "constraint_type": constraint.constraint_type,
                    "column_names": list(constraint.column_names),
                    "referenced_schema_name": constraint.referenced_schema_name,
                    "referenced_relation_name": constraint.referenced_relation_name,
                    "referenced_column_names": list(constraint.referenced_column_names),
                }
                for constraint in relation.constraints
            ],
        }
        for relation in binding.catalog.relations
        if (relation.schema_name, relation.relation_name) in requested
    ]


async def authorized_relation_keys(
    session: AsyncSession, identity: CurrentIdentity, snapshot_id: int | None = None
) -> frozenset[tuple[str, str]]:
    """Small query-planner helper that avoids exposing catalog details."""

    return (await authorized_schema(session, identity, snapshot_id=snapshot_id)).relation_keys


__all__ = [
    "MAX_COMMENT_CHARS",
    "BoundSchema",
    "SchemaBinding",
    "authorized_relation_keys",
    "authorized_schema",
    "catalog_projection",
    "detail_projection",
    "revalidate_schema",
    "reauthorize_schema",
    "sanitize_comment",
]
