"""Deterministic Phase-8 SQL/MCP semantics against the synthetic business DB."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from phase8_helpers import Phase8MCPFixture
from psycopg import sql

from enterprise_mind.database.contracts import SchemaCatalog, SchemaRelation
from enterprise_mind.database.guard import SQLGuard
from enterprise_mind.database.mcp_client import DatabaseMCPClient

pytest_plugins = ("phase8_helpers",)
pytestmark = pytest.mark.integration


def _relation(catalog: SchemaCatalog, schema: str, name: str) -> SchemaRelation:
    relation = catalog.get(schema, name)
    assert relation is not None
    return relation


def _create_semantic_tables(
    fixture: Phase8MCPFixture,
) -> tuple[str, dict[str, str]]:
    business = fixture.business
    suffix = uuid4().hex[:10]
    names = {
        "groups": f"semantic_groups_{suffix}",
        "typed": f"semantic_types_{suffix}",
        "customers": f"semantic_customers_{suffix}",
        "lines": f"semantic_lines_{suffix}",
        "charges": f"semantic_charges_{suffix}",
    }
    with business.admin() as connection:
        schema_name = sql.Identifier(business.sales_schema)
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.{} ("
                "id bigint PRIMARY KEY, group_key text, amount numeric(18,2) NOT NULL)"
            ).format(schema_name, sql.Identifier(names["groups"]))
        )
        group_rows = [
            (index, f"group-{index:03d}", str(index) + ".00") for index in range(1, 122)
        ] + [(122, None, "122.00"), (123, None, "123.00")]
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL("INSERT INTO {}.{} (id, group_key, amount) VALUES (%s, %s, %s)").format(
                    schema_name, sql.Identifier(names["groups"])
                ),
                group_rows,
            )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.{} ("
                "id bigint PRIMARY KEY, big_value bigint NOT NULL, "
                "decimal_value numeric(24,4) NOT NULL, optional_text text, "
                "observed_at timestamptz NOT NULL)"
            ).format(schema_name, sql.Identifier(names["typed"]))
        )
        connection.execute(
            sql.SQL(
                "INSERT INTO {}.{} "
                "(id, big_value, decimal_value, optional_text, observed_at) "
                "VALUES (%s, %s, %s, %s, %s)"
            ).format(schema_name, sql.Identifier(names["typed"])),
            (1, 9223372036854775807, Decimal("123456789012.3400"), None, "2026-01-02 03:04:05+08"),
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.{} (id bigint PRIMARY KEY, business_key text NOT NULL UNIQUE)"
            ).format(schema_name, sql.Identifier(names["customers"]))
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.{} ("
                "id bigint PRIMARY KEY, customer_id bigint NOT NULL, amount numeric(18,2) NOT NULL)"
            ).format(schema_name, sql.Identifier(names["lines"]))
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.{} ("
                "id bigint PRIMARY KEY, customer_id bigint NOT NULL, amount numeric(18,2) NOT NULL)"
            ).format(schema_name, sql.Identifier(names["charges"]))
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL("INSERT INTO {}.{} (id, business_key) VALUES (%s, %s)").format(
                    schema_name, sql.Identifier(names["customers"])
                ),
                [(1, "customer-a"), (2, "customer-b")],
            )
            cursor.executemany(
                sql.SQL("INSERT INTO {}.{} (id, customer_id, amount) VALUES (%s, %s, %s)").format(
                    schema_name, sql.Identifier(names["lines"])
                ),
                [(11, 1, "10.00"), (12, 1, "20.00"), (21, 2, "30.00")],
            )
            cursor.executemany(
                sql.SQL("INSERT INTO {}.{} (id, customer_id, amount) VALUES (%s, %s, %s)").format(
                    schema_name, sql.Identifier(names["charges"])
                ),
                [(101, 1, "5.00"), (102, 1, "7.00"), (201, 2, "11.00")],
            )
        for table_name in names.values():
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                    schema_name, sql.Identifier(table_name), sql.Identifier(business.role_name)
                )
            )
    return business.sales_schema, names


def _drop_semantic_tables(fixture: Phase8MCPFixture, names: dict[str, str]) -> None:
    with fixture.business.admin() as connection:
        for table_name in names.values():
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{} CASCADE").format(
                    sql.Identifier(fixture.business.sales_schema), sql.Identifier(table_name)
                )
            )


@pytest.mark.asyncio
async def test_unbounded_date_range_keeps_predicates_across_real_mcp_pages(
    phase8_mcp: Phase8MCPFixture,
) -> None:
    """Removing a model-added row cap must preserve both date boundaries."""

    business = phase8_mcp.business
    table_name = f"semantic_dates_{uuid4().hex[:10]}"
    relation_name = sql.SQL("{}.{}").format(
        sql.Identifier(business.sales_schema), sql.Identifier(table_name)
    )
    with business.admin() as connection:
        connection.execute(
            sql.SQL("CREATE TABLE {} (id bigint PRIMARY KEY, order_date date NOT NULL)").format(
                relation_name
            )
        )
        connection.execute(
            sql.SQL(
                "INSERT INTO {} (id, order_date) "
                "SELECT id, DATE '2026-01-01' + ((id - 1) % 28) "
                "FROM generate_series(1, 235) AS id"
            ).format(relation_name)
        )
        connection.execute(
            sql.SQL(
                "INSERT INTO {} (id, order_date) VALUES (0, '2025-12-31'), (236, '2026-02-01')"
            ).format(relation_name)
        )
        connection.execute(
            sql.SQL("GRANT SELECT ON {} TO {}").format(
                relation_name, sql.Identifier(business.role_name)
            )
        )
    client = DatabaseMCPClient(phase8_mcp.endpoint, business.token, source=business.source_metadata)
    try:
        catalog = await client.fetch_schema()
        relation = _relation(catalog, business.sales_schema, table_name)
        scoped = SchemaCatalog((relation,))
        guard = SQLGuard(scoped)
        query = (
            f'SELECT "id" FROM {relation.quoted_qualified_name} '
            'WHERE "order_date" >= $1 AND "order_date" < $2 ORDER BY "id" LIMIT 50'
        )
        pages = []
        ids = []
        for page_index in range(3):
            guarded = guard.validate(
                query, ("2026-01-01", "2026-02-01"), page_size=100, page_index=page_index
            )
            corrected = guard.without_top_level_limit(guarded)
            assert corrected is not None
            assert corrected.has_business_limit is False
            assert corrected.validated_parameters == ("2026-01-01", "2026-02-01")
            assert "order_date >= $1" in corrected.validated_query
            assert "order_date < $2" in corrected.validated_query
            result = await client.execute_query(corrected, scoped, 1)
            pages.append((result.row_count, result.has_more))
            ids.extend(int(row["id"]) for row in result.rows)
        assert pages == [(100, True), (100, True), (35, False)]
        assert ids == list(range(1, 236))
    finally:
        await client.close()
        with business.admin() as connection:
            connection.execute(sql.SQL("DROP TABLE {}").format(relation_name))


@pytest.mark.asyncio
async def test_phase8_sql_semantics_against_real_postgres_mcp(
    phase8_mcp: Phase8MCPFixture,
) -> None:
    business = phase8_mcp.business
    schema_name, auxiliary = _create_semantic_tables(phase8_mcp)
    client = DatabaseMCPClient(
        phase8_mcp.endpoint,
        business.token,
        source=business.source_metadata,
    )
    try:
        catalog = await client.fetch_schema()
        orders = _relation(catalog, business.sales_schema, business.orders_table)
        groups = _relation(catalog, schema_name, auxiliary["groups"])
        typed = _relation(catalog, schema_name, auxiliary["typed"])
        customers = _relation(catalog, schema_name, auxiliary["customers"])
        lines = _relation(catalog, schema_name, auxiliary["lines"])
        charges = _relation(catalog, schema_name, auxiliary["charges"])

        async def execute(
            query: str,
            relations: tuple[SchemaRelation, ...],
            parameters: tuple[object, ...] = (),
            *,
            page_size: int = 100,
            page_index: int = 0,
        ):
            scoped = SchemaCatalog(relations)
            guarded = SQLGuard(scoped).validate(
                query,
                parameters,
                page_size=page_size,
                page_index=page_index,
            )
            return await client.execute_query(guarded, scoped, 1)

        orders_sql = orders.quoted_qualified_name
        boundary_counts = {
            0: (0, False),
            1: (1, False),
            99: (99, False),
            100: (100, False),
        }
        for limit, expected in boundary_counts.items():
            result = await execute(
                f'SELECT "id" FROM {orders_sql} WHERE "id" <= $1 ORDER BY "id"',
                (orders,),
                (limit,),
            )
            assert (result.row_count, result.has_more) == expected
        expected_boundaries = (
            (101, ((100, True), (1, False))),
            (235, ((100, True), (100, True), (35, False))),
        )
        for total, expected_pages in expected_boundaries:
            pages = []
            seen = []
            for page_index in range(len(expected_pages)):
                result = await execute(
                    f'SELECT "id" FROM {orders_sql} WHERE "id" <= $1 ORDER BY "id"',
                    (orders,),
                    (total,),
                    page_index=page_index,
                )
                pages.append((result.row_count, result.has_more))
                seen.extend(int(row["id"]) for row in result.rows)
            assert pages == list(expected_pages)
            assert seen == list(range(1, total + 1))

        top10 = await execute(
            f'SELECT "id", "amount" FROM {orders_sql} ORDER BY "amount" DESC, "id" DESC LIMIT 10',
            (orders,),
        )
        assert top10.row_count == 10 and top10.has_more is False
        assert [int(row["id"]) for row in top10.rows] == list(range(235, 225, -1))
        inner_limit = await execute(
            f'SELECT count(*) AS "selected_count" FROM '
            f'(SELECT "id" FROM {orders_sql} ORDER BY "id" LIMIT 200) AS selected',
            (orders,),
            page_size=1,
        )
        assert inner_limit.rows[0]["selected_count"] == "200"

        injection = await execute(
            f'SELECT "id" FROM {orders_sql} WHERE "status" = $1 ORDER BY "id"',
            (orders,),
            ('paid\'; DELETE FROM "orders"; DROP TABLE "orders"; --',),
        )
        assert injection.row_count == 0 and injection.has_more is False
        still_there = await execute(
            f'SELECT "id" FROM {orders_sql} WHERE "id" = $1 ORDER BY "id"',
            (orders,),
            (1,),
            page_size=1,
        )
        assert still_there.row_count == 1

        group_sql = (
            f'SELECT "group_key", count(*) AS "item_count", sum("amount") AS "amount_total" '
            f'FROM {groups.quoted_qualified_name} GROUP BY "group_key" ORDER BY "group_key"'
        )
        group_pages = []
        group_keys: list[str | None] = []
        for page_index in (0, 1):
            result = await execute(group_sql, (groups,), page_index=page_index)
            group_pages.append((result.row_count, result.has_more))
            group_keys.extend(row["group_key"] for row in result.rows)
        assert group_pages == [(100, True), (22, False)]
        assert len(group_keys) == 122 and len(set(group_keys)) == 122
        assert group_keys.count(None) == 1

        typed_result = await execute(
            f'SELECT "big_value", "decimal_value", "optional_text", "observed_at" '
            f'FROM {typed.quoted_qualified_name} ORDER BY "id"',
            (typed,),
            page_size=1,
        )
        typed_row = typed_result.rows[0]
        assert typed_row["big_value"] == "9223372036854775807"
        assert typed_row["decimal_value"] == "123456789012.3400"
        assert typed_row["optional_text"] is None
        assert typed_row["observed_at"].endswith("+00:00")

        join_sql = (
            "WITH line_totals AS ("
            f'SELECT "customer_id", count(*) AS "line_count", sum("amount") AS "line_total" '
            f'FROM {lines.quoted_qualified_name} GROUP BY "customer_id"), '
            "charge_totals AS ("
            f'SELECT "customer_id", count(*) AS "charge_count", sum("amount") AS "charge_total" '
            f'FROM {charges.quoted_qualified_name} GROUP BY "customer_id") '
            f'SELECT c."business_key", l."line_count", l."line_total", '
            f'x."charge_count", x."charge_total" FROM {customers.quoted_qualified_name} AS c '
            f'JOIN line_totals AS l ON l."customer_id" = c."id" '
            f'JOIN charge_totals AS x ON x."customer_id" = c."id" '
            'ORDER BY c."business_key"'
        )
        join_result = await execute(join_sql, (customers, lines, charges), page_size=10)
        assert join_result.rows == (
            {
                "business_key": "customer-a",
                "line_count": "2",
                "line_total": "30.00",
                "charge_count": "2",
                "charge_total": "12.00",
            },
            {
                "business_key": "customer-b",
                "line_count": "1",
                "line_total": "30.00",
                "charge_count": "1",
                "charge_total": "11.00",
            },
        )
    finally:
        await client.close()
        _drop_semantic_tables(phase8_mcp, auxiliary)
