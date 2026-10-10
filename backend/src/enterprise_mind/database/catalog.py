"""Fixed catalog reads. No caller SQL, row samples, or credentials are returned."""

import hashlib
import re
from dataclasses import asdict
from typing import Any, cast

from enterprise_mind.database.contracts import (
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraint,
    SchemaRelation,
    relation_signature,
)

RELATIONS_SQL = """
SELECT c.oid::bigint AS relation_oid, n.nspname, c.relname, c.relkind,
       pg_catalog.obj_description(c.oid, 'pg_class') AS comment,
       CASE WHEN c.relkind = 'v'
            THEN pg_catalog.pg_get_viewdef(c.oid, true)
            ELSE NULL
       END AS view_definition
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p','v') AND n.nspname <> 'information_schema'
 AND n.nspname !~ '^pg_' AND pg_catalog.has_table_privilege(c.oid, 'SELECT')
ORDER BY n.nspname, c.relname LIMIT 10001
"""
COLUMNS_SQL = """
SELECT a.attrelid, a.attnum, a.attname, NOT a.attnotnull AS is_nullable,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       pg_catalog.col_description(a.attrelid, a.attnum) AS comment
FROM pg_catalog.pg_attribute a WHERE a.attrelid = ANY($1::oid[])
 AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attrelid, a.attnum LIMIT 100001
"""
CONSTRAINTS_SQL = """
SELECT c.conrelid, c.conname, c.contype, c.conkey, c.confkey, c.confrelid,
       rn.nspname AS referenced_schema, rc.relname AS referenced_relation
FROM pg_catalog.pg_constraint c
LEFT JOIN pg_catalog.pg_class rc ON rc.oid = c.confrelid
LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace
WHERE c.conrelid = ANY($1::oid[]) AND c.contype IN ('p','u','f')
ORDER BY c.conrelid, c.conname LIMIT 100001
"""


def _view_definition_signature(value: Any, *, already_hashed: bool = False) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        value = str(value)
    if already_hashed and len(value) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return cast(str, value).lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def fetch_catalog(connection: Any) -> SchemaCatalog:
    relations = await connection.fetch(RELATIONS_SQL)
    if len(relations) > 10000:
        raise ValueError("SCHEMA_SNAPSHOT_UNAVAILABLE")
    ids = [row.get("relation_oid", row.get("oid")) for row in relations]
    if any(value is None for value in ids):
        raise ValueError("SCHEMA_SNAPSHOT_UNAVAILABLE")
    columns = await connection.fetch(COLUMNS_SQL, ids)
    constraints = await connection.fetch(CONSTRAINTS_SQL, ids)
    if len(columns) > 100000 or len(constraints) > 100000:
        raise ValueError("SCHEMA_SNAPSHOT_UNAVAILABLE")
    names = {(c["attrelid"], c["attnum"]): c["attname"] for c in columns}
    columns_by_relation: dict[int, list[Any]] = {}
    constraints_by_relation: dict[int, list[Any]] = {}
    for column in columns:
        columns_by_relation.setdefault(column["attrelid"], []).append(column)
    for constraint in constraints:
        constraints_by_relation.setdefault(constraint["conrelid"], []).append(constraint)
    output = []
    for relation in relations:
        oid = relation.get("relation_oid", relation.get("oid"))
        if oid is None:
            raise ValueError("SCHEMA_SNAPSHOT_UNAVAILABLE")
        relation_constraints = []
        for c in constraints_by_relation.get(oid, []):
            foreign = c["contype"] == "f"
            unique = c["contype"] == "u"
            # A FK to a relation invisible to the readonly role is not catalog content.
            if foreign and (
                c["confrelid"] is None
                or c["confkey"] is None
                or any((c["confrelid"], n) not in names for n in c["confkey"])
            ):
                continue
            relation_constraints.append(
                SchemaConstraint(
                    name=c["conname"],
                    constraint_type=(
                        "foreign_key" if foreign else "unique" if unique else "primary_key"
                    ),
                    column_names=tuple(names[(oid, n)] for n in c["conkey"]),
                    referenced_schema_name=c["referenced_schema"] if foreign else None,
                    referenced_relation_name=c["referenced_relation"] if foreign else None,
                    referenced_column_names=tuple(names[(c["confrelid"], n)] for n in c["confkey"])
                    if foreign
                    else (),
                )
            )
        unique_columns = {
            constraint.column_names[0]
            for constraint in relation_constraints
            if constraint.constraint_type in {"primary_key", "unique"}
            and len(constraint.column_names) == 1
            and (
                constraint.constraint_type == "primary_key"
                or next(
                    (
                        column.get("is_nullable") is False
                        for column in columns_by_relation.get(oid, [])
                        if column["attname"] == constraint.column_names[0]
                    ),
                    False,
                )
            )
        }
        columns_for_relation = tuple(
            SchemaColumn(
                c["attname"],
                c["data_type"],
                c["comment"],
                c["attnum"],
                c.get("is_nullable"),
                c["attname"] in unique_columns,
            )
            for c in columns_by_relation.get(oid, [])
        )
        raw_oid = relation.get("relation_oid", relation.get("oid"))
        normalized = SchemaRelation(
            schema_name=relation["nspname"],
            relation_name=relation["relname"],
            relation_type="view" if relation["relkind"] == "v" else "table",
            comment=relation["comment"],
            columns=columns_for_relation,
            constraints=tuple(relation_constraints),
            relation_oid=int(raw_oid) if raw_oid is not None else None,
            view_definition_signature=(
                _view_definition_signature(
                    relation["view_definition"]
                    if relation.get("view_definition") is not None
                    else relation.get("view_definition_signature"),
                    already_hashed=relation.get("view_definition") is None,
                )
                if relation["relkind"] == "v"
                else None
            ),
        )
        if normalized.relation_type == "view" and normalized.view_definition_signature is None:
            raise ValueError("SCHEMA_SNAPSHOT_UNAVAILABLE")
        output.append(
            SchemaRelation(
                schema_name=normalized.schema_name,
                relation_name=normalized.relation_name,
                relation_type=normalized.relation_type,
                columns=normalized.columns,
                comment=normalized.comment,
                constraints=normalized.constraints,
                relation_oid=normalized.relation_oid,
                view_definition_signature=normalized.view_definition_signature,
                relation_signature=relation_signature(normalized),
            )
        )
    return SchemaCatalog(tuple(output))


def catalog_to_wire(catalog: SchemaCatalog) -> dict[str, Any]:
    return asdict(catalog)


def catalog_from_wire(value: dict[str, Any]) -> SchemaCatalog:
    if set(value) != {"relations"} or not isinstance(value["relations"], list):
        raise ValueError("invalid catalog")
    relations = []
    for raw in value["relations"]:
        row = dict(raw)
        row["columns"] = tuple(SchemaColumn(**c) for c in row["columns"])
        row["constraints"] = tuple(SchemaConstraint(**c) for c in row.get("constraints", []))
        relations.append(SchemaRelation(**row))
    return SchemaCatalog(tuple(relations))
