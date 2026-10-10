from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from enterprise_mind.database.contracts import (
    GeneratedQuery,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraint,
    SchemaRelation,
)
from enterprise_mind.database.guard import (
    SQLGuard,
    SQLGuardError,
    infer_query_kind,
    summarize_conditions,
)
from enterprise_mind.database.query import is_bounded_result_request
from enterprise_mind.database.results import (
    ResultColumn,
    ResultTooLargeError,
    normalize_query_result,
)


@pytest.fixture
def catalog() -> SchemaCatalog:
    return SchemaCatalog(
        (
            SchemaRelation(
                "public",
                "orders",
                columns=(
                    SchemaColumn("id", "bigint", ordinal=1),
                    SchemaColumn("uid", "bigint", ordinal=2),
                    SchemaColumn("amount", "numeric", ordinal=3),
                    SchemaColumn("user_id", "bigint", ordinal=4),
                    SchemaColumn("name", "text", ordinal=5),
                    SchemaColumn("ts", "timestamptz", ordinal=6),
                ),
                constraints=(SchemaConstraint("orders_pkey", "primary_key", ("id",)),),
            ),
            SchemaRelation(
                "public",
                "users",
                columns=(
                    SchemaColumn("id", "bigint", ordinal=1),
                    SchemaColumn("name", "text", ordinal=2),
                ),
            ),
        )
    )


def test_generated_query_and_guarded_query_repr_redact_query_material(catalog: SchemaCatalog):
    generated = GeneratedQuery(
        'SELECT id FROM "public"."orders" WHERE id=$1',
        ("sensitive",),
        conditions_summary="id=$1 and sensitive",
    )
    assert "sensitive" not in repr(generated)
    assert "SELECT" not in repr(generated)
    guarded = SQLGuard(catalog).validate(generated)
    rendered = repr(guarded)
    assert "SELECT" not in rendered
    assert "sensitive" not in rendered
    assert "<redacted>" in rendered


def test_valid_query_is_paged_and_keeps_original_for_mcp_reparse(catalog: SchemaCatalog):
    guarded = SQLGuard(catalog).validate(
        'SELECT o.id FROM "public"."orders" o WHERE o.id=$1 ORDER BY o.id',
        (7,),
        page_size=100,
        page_index=1,
    )
    assert guarded.referenced_relations == ('"public"."orders"',)
    assert guarded.parameters == (7, 101, 100)
    assert "$2" in guarded.sql and "$3" in guarded.sql
    assert "LIMIT" not in guarded.validated_query.upper()
    replay = SQLGuard(catalog).validate(guarded.validated_query, guarded.validated_parameters)
    assert replay.referenced_relations == guarded.referenced_relations


def test_group_paging_does_not_merge_same_named_columns_from_different_relations(catalog):
    query = (
        "SELECT o.id AS order_id, u.id AS user_id, count(*) AS total "
        'FROM "public"."orders" o JOIN "public"."users" u ON o.uid=u.id '
        "GROUP BY o.id, u.id ORDER BY o.id"
    )
    with pytest.raises(SQLGuardError):
        SQLGuard(catalog).validate(query, page_index=1)
    assert SQLGuard(catalog).validate(query + ", u.id", page_index=1).page_index == 1
    assert (
        SQLGuard(catalog)
        .validate('SELECT DISTINCT name FROM "public"."orders" ORDER BY name', page_index=1)
        .page_index
        == 1
    )


def test_group_paging_rejects_order_by_aggregate_alias_shadowing_group_key(catalog):
    query = (
        "SELECT o.amount AS grouping_value, count(*) AS amount "
        'FROM "public"."orders" o JOIN "public"."users" u ON o.uid=u.id '
        "GROUP BY amount ORDER BY amount"
    )
    with pytest.raises(SQLGuardError):
        SQLGuard(catalog).validate(query, page_index=1)


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        ('SELECT COUNT(*) AS total FROM "public"."orders"', ()),
        ('SELECT COUNT(*), SUM(amount) FROM "public"."orders"', ()),
        ('SELECT NULL AS nullable FROM "public"."orders"', ()),
        ('SELECT o.id, u.name FROM "public"."orders" o JOIN "public"."users" u ON o.uid=u.id', ()),
        (
            'WITH recent AS (SELECT id, user_id FROM "public"."orders" WHERE id>$1) '
            "SELECT r.id FROM recent r WHERE EXISTS "
            '(SELECT u.id FROM "public"."users" u WHERE u.id=r.user_id)',
            (3,),
        ),
        (
            "WITH values_cte(first_value, second_value) AS "
            '(SELECT id, id FROM "public"."orders") '
            "SELECT first_value, second_value FROM values_cte",
            (),
        ),
        (
            "SELECT sum(amount) FILTER (WHERE amount>0) OVER "
            '(PARTITION BY user_id ORDER BY id) AS total FROM "public"."orders"',
            (),
        ),
        (
            "SELECT lower(name) AS lower_name, trim(name) AS trimmed_name, "
            'round(amount, 2) AS rounded_amount FROM "public"."orders"',
            (),
        ),
        ('SELECT id::bigint AS id_bigint, id::text AS id_text FROM "public"."orders"', ()),
        ('SELECT id FROM "public"."orders" WHERE id BETWEEN $1 AND $2', (1, 10)),
        ('SELECT id FROM "public"."orders" WHERE id IN ($1, $2)', (1, 2)),
        ('SELECT id FROM "public"."orders" WHERE id IS DISTINCT FROM $1', (None,)),
        (
            "SELECT 'a;b' AS literal_one, $$dollar;quoted$$ AS literal_two "
            'FROM "public"."orders"',
            (),
        ),
    ],
)
def test_allows_read_only_ast_constructs(
    catalog: SchemaCatalog, sql: str, parameters: tuple[object, ...]
):
    result = SQLGuard(catalog).validate(sql, parameters)
    assert (
        result.referenced_relations == ('"public"."orders"',)
        or len(result.referenced_relations) == 2
    )


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT id FROM "public"."orders"; DROP TABLE "public"."orders"',
        'INSERT INTO "public"."orders" (id) VALUES (1)',
        'WITH removed AS (DELETE FROM "public"."orders" RETURNING id) SELECT id FROM removed',
        'SELECT id INTO "public"."copy" FROM "public"."orders"',
        'SELECT id FROM "public"."orders" FOR UPDATE',
        'SELECT id FROM "public"."orders" OFFSET 1',
        'SELECT * FROM "public"."orders"',
        'SELECT o.* FROM "public"."orders" o',
        "SELECT id FROM orders",
        'SELECT id FROM "pg_catalog"."pg_class"',
        'SELECT pg_sleep(1) FROM "public"."orders"',
        'SELECT nextval(\'order_seq\') FROM "public"."orders"',
        'SELECT id FROM "public"."orders" WHERE id OPERATOR(public.=) 1',
        'SELECT id::"public"."custom_type" FROM "public"."orders"',
        'WITH hidden AS (SELECT id FROM "public"."secret") SELECT id FROM hidden',
    ],
)
def test_rejects_writes_system_objects_and_ast_bypass(catalog: SchemaCatalog, sql: str):
    with pytest.raises(SQLGuardError) as error:
        SQLGuard(catalog).validate(sql)
    assert sql not in str(error.value)
    assert error.value.reason == str(error.value)


def test_rejects_parameter_gaps_and_non_scalar_values(catalog: SchemaCatalog):
    guard = SQLGuard(catalog)
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" WHERE id=$2', (1,))
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" WHERE id=$1', ([1],))
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders"', (1,))


def test_business_limit_is_bounded_and_keeps_nested_semantics(catalog: SchemaCatalog):
    guard = SQLGuard(catalog)
    top_n = guard.validate('SELECT id FROM "public"."orders" ORDER BY id LIMIT 10')
    assert "LIMIT 10" in top_n.validated_query
    assert top_n.has_business_limit is True
    assert top_n.parameters == (101, 0)
    parameterized = guard.validate('SELECT id FROM "public"."orders" ORDER BY id LIMIT $1', (200,))
    assert parameterized.validated_parameters == (200,)
    nested = guard.validate(
        "SELECT count(*) AS total FROM ("
        'SELECT id FROM "public"."orders" ORDER BY id LIMIT 200'
        ") AS selected"
    )
    assert "LIMIT 200" in nested.validated_query
    assert nested.has_business_limit is True
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" LIMIT 10001')
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" LIMIT ALL')
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" LIMIT $1', (10001,))
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" FETCH FIRST 10 ROWS ONLY')


def test_only_removable_outer_limit_can_be_removed_after_guard(catalog: SchemaCatalog):
    guard = SQLGuard(catalog)
    top = guard.validate('SELECT id FROM "public"."orders" ORDER BY id LIMIT 50')
    corrected = guard.without_top_level_limit(top)
    assert corrected is not None
    assert corrected.has_business_limit is False
    assert corrected.referenced_relations == top.referenced_relations
    assert "LIMIT" not in corrected.validated_query

    parameterized = guard.validate('SELECT id FROM "public"."orders" ORDER BY id LIMIT $1', (50,))
    parameterized_corrected = guard.without_top_level_limit(parameterized)
    assert parameterized_corrected is not None
    assert parameterized_corrected.validated_parameters == ()
    assert parameterized_corrected.has_business_limit is False

    shared_parameter = guard.validate(
        'SELECT id FROM "public"."orders" WHERE id > $1 ORDER BY id LIMIT $1', (50,)
    )
    assert guard.without_top_level_limit(shared_parameter) is None

    nested = guard.validate(
        'SELECT id FROM (SELECT id FROM "public"."orders" ORDER BY id LIMIT 50) AS selected '
        "ORDER BY id LIMIT 100"
    )
    assert guard.without_top_level_limit(nested) is None


@pytest.mark.parametrize(
    ("question", "bounded"),
    [
        ("查询全部订单，每页返回 100 行。", False),
        ("查询全部订单，每页最多 100 行。", False),
        ("分页时每页展示最多 100 条记录。", False),
        ("查询全部订单，按 id 升序。", False),
        ("查询前 10 条订单。", True),
        ("返回 Top 20 订单。", True),
        ("只要最多 5 条结果。", True),
    ],
)
def test_business_limit_detection_preserves_first_n_semantics(question, bounded):
    assert is_bounded_result_request(question) is bounded


def test_condition_summary_and_query_kind_use_guarded_shape(catalog: SchemaCatalog):
    query = 'SELECT id FROM "public"."orders" WHERE id=$1 AND name=$2 ORDER BY id'
    guarded = SQLGuard(catalog).validate(query, (7, "private"))
    summary = summarize_conditions(guarded.validated_query)
    assert "id" in summary and "name" in summary
    assert "$1" not in summary and "private" not in summary
    assert infer_query_kind(guarded.validated_query) == "detail"
    assert infer_query_kind('SELECT count(*) AS total FROM "public"."orders"') == "aggregate"


def test_second_page_requires_server_checkable_ordering(catalog: SchemaCatalog):
    guard = SQLGuard(catalog)
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders"', page_index=1)
    unstable_first = guard.validate('SELECT id FROM "public"."orders"')
    assert guard.continuation_available(unstable_first) is False
    stable_first = guard.validate('SELECT id FROM "public"."orders" ORDER BY id')
    assert guard.continuation_available(stable_first) is True
    assert (
        guard.validate('SELECT id FROM "public"."orders" ORDER BY id', page_index=1).page_index == 1
    )
    assert (
        guard.validate(
            'SELECT id AS order_id FROM "public"."orders" ORDER BY order_id', page_index=1
        ).page_index
        == 1
    )
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders"', page_size=101)
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" ORDER BY amount', page_index=1)
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT amount AS id FROM "public"."orders" ORDER BY id', page_index=1)
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT id FROM "public"."orders" ORDER BY 1', page_index=1)
    with pytest.raises(SQLGuardError):
        guard.validate(
            'SELECT id FROM "public"."orders" ORDER BY id USING OPERATOR(public.<)',
            page_index=1,
        )


def test_composite_unique_key_proves_order_only_when_all_key_columns_are_nonnull():
    relation = SchemaRelation(
        "public",
        "unique_orders",
        columns=(
            SchemaColumn("tenant_id", "bigint", ordinal=1, is_nullable=False),
            SchemaColumn("order_no", "bigint", ordinal=2, is_nullable=False),
        ),
        constraints=(SchemaConstraint("uq", "unique", ("tenant_id", "order_no")),),
    )
    guard = SQLGuard(SchemaCatalog((relation,)))
    assert (
        guard.validate(
            'SELECT tenant_id, order_no FROM "public"."unique_orders" ORDER BY tenant_id, order_no',
            page_index=1,
        ).page_index
        == 1
    )
    nullable_relation = SchemaRelation(
        "public",
        "nullable_unique_orders",
        columns=(
            SchemaColumn("tenant_id", "bigint", ordinal=1, is_nullable=False),
            SchemaColumn("order_no", "bigint", ordinal=2, is_nullable=True),
        ),
        constraints=(SchemaConstraint("uq", "unique", ("tenant_id", "order_no")),),
    )
    with pytest.raises(SQLGuardError):
        SQLGuard(SchemaCatalog((nullable_relation,))).validate(
            'SELECT tenant_id, order_no FROM "public"."nullable_unique_orders" '
            "ORDER BY tenant_id, order_no",
            page_index=1,
        )


def test_set_operation_ordering_uses_set_output_scope(catalog: SchemaCatalog):
    guard = SQLGuard(catalog)
    query = (
        'SELECT id AS value FROM "public"."orders" '
        'UNION SELECT id AS value FROM "public"."orders" ORDER BY value'
    )
    assert guard.validate(query, page_index=1).page_index == 1


def test_custom_snapshot_column_type_is_rejected(catalog: SchemaCatalog):
    custom = SchemaCatalog(
        catalog.relations
        + (
            SchemaRelation(
                "public",
                "unsafe_values",
                columns=(SchemaColumn("value", "public.secret_domain", ordinal=1),),
            ),
        )
    )
    with pytest.raises(SQLGuardError):
        SQLGuard(custom).validate('SELECT value FROM "public"."unsafe_values"')


def test_duplicate_result_names_are_rejected_before_row_mapping(catalog: SchemaCatalog):
    with pytest.raises(SQLGuardError):
        SQLGuard(catalog).validate(
            'SELECT o.id, u.id FROM "public"."orders" o JOIN "public"."users" u ON o.uid=u.id'
        )
    with pytest.raises(ValueError):
        normalize_query_result(
            [("id", "bigint"), ("id", "bigint")],
            [(1, 2)],
        )


def test_result_normalization_drops_bytea_and_preserves_complete_rows():
    result = normalize_query_result(
        [
            ResultColumn("id", "bigint"),
            ResultColumn("amount", "numeric"),
            ResultColumn("payload", "bytea"),
            ResultColumn("created", "timestamptz"),
        ],
        [
            (9223372036854775807, Decimal("12.3400"), b"secret", datetime(2026, 1, 1, tzinfo=UTC)),
            (2, Decimal("1.0"), b"secret2", datetime(2026, 1, 2, tzinfo=UTC)),
            (3, Decimal("2.0"), b"secret3", datetime(2026, 1, 3, tzinfo=UTC)),
        ],
        page_size=2,
        referenced_relations=('"public"."orders"',),
    )
    assert [column.name for column in result.columns] == ["id", "amount", "created"]
    assert result.rows[0]["id"] == "9223372036854775807"
    assert result.rows[0]["amount"] == "12.3400"
    assert result.rows[0]["created"].endswith("+00:00")
    assert "payload" not in result.rows[0]
    assert result.row_count == 2
    assert result.has_more is True


def test_result_budget_stops_before_oversized_complete_row():
    with pytest.raises(ResultTooLargeError):
        normalize_query_result(
            [("id", "bigint"), ("body", "text")],
            [(1, "x" * 20), (2, "y" * 20)],
            page_size=100,
            max_bytes=180,
        )
    with pytest.raises(ValueError):
        normalize_query_result(
            [("m" * 100, "text")],
            [],
            max_bytes=100,
        )
    with pytest.raises(ValueError):
        normalize_query_result(
            [("id", "bigint")],
            [(1,)],
            max_bytes=1024 * 1024 + 1,
        )


def test_result_budget_uses_the_final_has_more_flag():
    payload = {
        "columns": [{"name": "id", "data_type": "bigint"}],
        "rows": [{"id": "1"}],
        "row_count": 1,
        "has_more": False,
        "referenced_relations": (),
        "elapsed_ms": 0.0,
    }
    exact_budget = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    result = normalize_query_result(
        [("id", "bigint")],
        [(1,)],
        max_bytes=exact_budget,
    )
    assert result.rows == ({"id": "1"},)
    assert result.has_more is False
