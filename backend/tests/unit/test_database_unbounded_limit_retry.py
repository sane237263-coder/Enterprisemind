import pytest

from enterprise_mind.database import query as query_module
from enterprise_mind.database.contracts import (
    QueryResult,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraint,
    SchemaRelation,
)
from enterprise_mind.database.projection import BoundSchema
from enterprise_mind.database.query import DatabaseQueryService, PreparedQuery
from enterprise_mind.database.results import ResultColumn
from enterprise_mind.database.sql_provider import SQLDraft

SQL = 'SELECT "id" FROM "business"."orders" ORDER BY "id"'
LIMITED_SQL = SQL + " LIMIT 50"
NESTED_LIMITED_SQL = (
    'SELECT id FROM (SELECT "id" FROM "business"."orders" ORDER BY "id" LIMIT 50) '
    "AS selected ORDER BY id"
)
RELATION = '"business"."orders"'


class QueryModel:
    def __init__(self, *, retry_sql: str, first_sql: str = LIMITED_SQL):
        self.first_sql = first_sql
        self.retry_sql = retry_sql
        self.calls = []

    async def generate_sql(
        self,
        question,
        semantics,
        schema,
        *,
        retry_without_business_limit=False,
        retry_guard_reason=None,
        retry_detail_required=False,
        retry_execution_rejected=False,
        current_time_utc=None,
    ):
        assert retry_guard_reason is None
        assert retry_detail_required is False
        assert retry_execution_rejected is False
        self.calls.append(
            (question, list(semantics), schema, retry_without_business_limit, current_time_utc)
        )
        sql = self.retry_sql if retry_without_business_limit else self.first_sql
        return SQLDraft(query_kind="detail", sql=sql)


class QueryTransport:
    def __init__(self):
        self.queries = []

    async def execute_query(self, query, catalog, snapshot_id):
        self.queries.append((query, catalog, snapshot_id))
        return QueryResult(
            columns=(ResultColumn("id", "bigint"),),
            rows=({"id": "1"},),
            row_count=1,
            has_more=False,
            referenced_relations=query.referenced_relations,
            elapsed_ms=1,
        )


def _prepared(question: str) -> PreparedQuery:
    relation = SchemaRelation(
        schema_name="business",
        relation_name="orders",
        columns=(SchemaColumn("id", "bigint", ordinal=1, is_nullable=False),),
        constraints=(SchemaConstraint("orders_pkey", "primary_key", ("id",)),),
        relation_oid=1001,
        relation_signature="authorized-orders-v1",
    )
    catalog = SchemaCatalog((relation,))
    binding = BoundSchema(
        database_source_id=7,
        source_name="Synthetic business source",
        database_name="business",
        schema_snapshot_id=9,
        snapshot_no=1,
        source_version=3,
        catalog=catalog,
        permission_fingerprint="department-grant-v1",
    )
    return PreparedQuery(binding, catalog, question, [], 100, 0)


class EmptySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_one_unbounded_limit_correction_reuses_authorized_query_context(monkeypatch):
    question = "请查询全部订单明细，每页返回 100 行。"
    prepared = _prepared(question)
    model = QueryModel(first_sql=NESTED_LIMITED_SQL, retry_sql=SQL)
    transport = QueryTransport()
    service = DatabaseQueryService(lambda: EmptySession(), model, transport)
    authorization_checks = []
    relation_checks = []

    async def authorized_schema(_session, _identity, snapshot_id=None):
        authorization_checks.append(snapshot_id)
        return prepared.binding

    async def revalidate_schema(_session, _identity, binding, referenced_relations):
        relation_checks.append((binding, referenced_relations))

    monkeypatch.setattr(query_module, "authorized_schema", authorized_schema)
    monkeypatch.setattr(query_module, "revalidate_schema", revalidate_schema)

    answer = await service.execute(object(), prepared)

    assert answer.result.row_count == 1
    assert len(model.calls) == 2
    original, correction = model.calls
    assert original[:3] == correction[:3]
    assert original[4] == correction[4]
    assert original[3] is False and correction[3] is True
    assert [(relation["schema_name"], relation["relation_name"]) for relation in original[2]] == [
        ("business", "orders")
    ]
    assert [column["name"] for column in original[2][0]["columns"]] == ["id"]
    assert len(transport.queries) == 1
    guarded = transport.queries[0][0]
    assert guarded.has_business_limit is False
    assert guarded.page_size == 100 and guarded.page_index == 0
    assert transport.queries[0][2] == prepared.binding.schema_snapshot_id
    assert authorization_checks == [prepared.binding.schema_snapshot_id] * 2
    assert relation_checks == [
        (prepared.binding, (RELATION,)),
        (prepared.binding, (RELATION,)),
    ]


@pytest.mark.asyncio
async def test_second_unrequested_top_level_limit_is_removed_before_transport(monkeypatch):
    prepared = _prepared("请查询全部订单，每页 100 行。")
    model = QueryModel(first_sql=NESTED_LIMITED_SQL, retry_sql=LIMITED_SQL)
    transport = QueryTransport()
    service = DatabaseQueryService(lambda: EmptySession(), model, transport)

    async def authorized_schema(_session, _identity, snapshot_id=None):
        return prepared.binding

    async def revalidate_schema(_session, _identity, _binding, _referenced_relations):
        return None

    monkeypatch.setattr(query_module, "authorized_schema", authorized_schema)
    monkeypatch.setattr(query_module, "revalidate_schema", revalidate_schema)

    answer = await service.execute(object(), prepared)

    assert answer.guarded_query is not None
    assert answer.guarded_query.has_business_limit is False
    assert len(model.calls) == 2
    assert model.calls[1][3] is True
    assert len(transport.queries) == 1
    assert "LIMIT" not in transport.queries[0][0].validated_query


@pytest.mark.asyncio
async def test_first_unrequested_top_level_limit_is_removed_without_model_retry(monkeypatch):
    prepared = _prepared("请查询全部订单，每页 100 行。")
    model = QueryModel(retry_sql=SQL)
    transport = QueryTransport()
    service = DatabaseQueryService(lambda: EmptySession(), model, transport)

    async def authorized_schema(_session, _identity, snapshot_id=None):
        return prepared.binding

    async def revalidate_schema(_session, _identity, _binding, _referenced_relations):
        return None

    monkeypatch.setattr(query_module, "authorized_schema", authorized_schema)
    monkeypatch.setattr(query_module, "revalidate_schema", revalidate_schema)

    answer = await service.execute(object(), prepared)

    assert answer.guarded_query is not None
    assert answer.guarded_query.has_business_limit is False
    assert len(model.calls) == 1
    assert len(transport.queries) == 1


@pytest.mark.asyncio
async def test_explicit_first_n_keeps_business_limit_without_correction(monkeypatch):
    prepared = _prepared("请查询前 50 条订单。")
    model = QueryModel(retry_sql=SQL)
    transport = QueryTransport()
    service = DatabaseQueryService(lambda: EmptySession(), model, transport)

    async def authorized_schema(_session, _identity, snapshot_id=None):
        return prepared.binding

    async def revalidate_schema(_session, _identity, _binding, _referenced_relations):
        return None

    monkeypatch.setattr(query_module, "authorized_schema", authorized_schema)
    monkeypatch.setattr(query_module, "revalidate_schema", revalidate_schema)

    answer = await service.execute(object(), prepared)

    assert answer.guarded_query is not None
    assert answer.guarded_query.has_business_limit is True
    assert len(model.calls) == 1
    assert model.calls[0][3] is False
    assert len(transport.queries) == 1
