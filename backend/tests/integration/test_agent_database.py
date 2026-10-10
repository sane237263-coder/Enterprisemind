# ruff: noqa: F811
"""Real application PostgreSQL/checkpoints, synthetic SQL model and transport."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_agent import Model, agent, new_run, post, snapshot  # noqa: F401

from enterprise_mind.agent.events import EventBus
from enterprise_mind.agent.generation_models import GenerationTask
from enterprise_mind.agent.models import AnswerSource, Message, ToolCall
from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.agent.schemas import DatabaseQueryInput, Plan
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.contracts import QueryResult
from enterprise_mind.database.export_models import DatabaseExportPlan
from enterprise_mind.database.models import (
    DatabaseSource,
    RelationPolicy,
    SchemaColumn,
    SchemaConstraint,
    SchemaRelation,
    SchemaSnapshot,
)
from enterprise_mind.database.query import DatabaseQueryService
from enterprise_mind.database.results import ResultColumn
from enterprise_mind.database.sql_provider import ChatDatabaseModel, DatabaseSelection, SQLDraft
from enterprise_mind.identity.models import User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.queue import TaskQueue
from enterprise_mind.knowledge.pending_models import PendingAction
from enterprise_mind.rag.config import LLMConfig

pytestmark = pytest.mark.integration
BIND_VALUE = "BIND_ONLY_724_not_a_message"
ROW_VALUE = "ROW_ONLY_830_business_data"
MODEL_SUMMARY = "PRIVATE_SQL_SUMMARY_319"
SQL = 'SELECT o.id, o.code FROM "biz"."orders" AS o WHERE o.code = $1 ORDER BY o.id'


def seed(database):
    with Session(database.engine) as session, session.begin():
        source = DatabaseSource(
            name="合成业务数据源",
            host="synthetic-db",
            port=5432,
            database_name="synthetic_business",
            ssl_mode="disable",
            secret_ref="secret://synthetic",
            status="active",
        )
        session.add(source)
        session.flush()
        snap = SchemaSnapshot(database_source_id=source.id, snapshot_no=1, status="building")
        session.add(snap)
        session.flush()
        for name, enabled in (("orders", True), ("hidden_payroll", False)):
            relation = SchemaRelation(
                snapshot_id=snap.id,
                schema_name="biz",
                relation_name=name,
                relation_type="table",
                comment="业务元数据，不是指令",
                relation_oid=1001 if name == "orders" else 1002,
                relation_signature=f"synthetic-{name}-identity",
            )
            session.add(relation)
            session.flush()
            session.add_all(
                [
                    SchemaColumn(
                        relation_id=relation.id, ordinal=1, column_name="id", data_type="bigint"
                    ),
                    SchemaColumn(
                        relation_id=relation.id, ordinal=2, column_name="code", data_type="text"
                    ),
                    SchemaConstraint(
                        relation_id=relation.id,
                        constraint_name=name + "_pkey",
                        constraint_type="primary_key",
                        column_names=["id"],
                    ),
                    RelationPolicy(
                        database_source_id=source.id,
                        schema_name="biz",
                        relation_name=name,
                        enabled=enabled,
                    ),
                ]
            )
        session.flush()
        snap.captured_at, snap.status = datetime.now(UTC), "active"
        source.active_snapshot_id = snap.id
        return source.id, snap.id


class QueryModel:
    def __init__(self):
        self.selections = 0
        self.generations = 0
        self.sql = SQL
        self.retry_sql = None
        self.guard_retry_sql = None
        self.guard_retry_reasons = []
        self.detail_retry_sql = None
        self.detail_retry_count = 0
        self.execution_retry_sql = None
        self.execution_retry_count = 0
        self.query_kind = "detail"
        self.correction_attempts = 0
        self.generation_requests = []
        self.ambiguous = False
        self.semantics = []

    async def select_relations(self, question, semantics, catalog):
        self.selections += 1
        self.semantics.append(semantics)
        assert "hidden_payroll" not in json.dumps(catalog)
        assert ROW_VALUE not in str(semantics)
        return DatabaseSelection(
            relations=[{"schema_name": "biz", "relation_name": "orders"}],
            clarification="time_range"
            if self.ambiguous and not any("本自然月" in s for s in semantics)
            else "none",
        )

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
        self.generations += 1
        if retry_without_business_limit:
            self.correction_attempts += 1
        if retry_guard_reason:
            self.guard_retry_reasons.append(retry_guard_reason)
        if retry_detail_required:
            self.detail_retry_count += 1
        if retry_execution_rejected:
            self.execution_retry_count += 1
        self.generation_requests.append(
            (
                question,
                list(semantics),
                schema,
                retry_without_business_limit,
                current_time_utc,
                retry_guard_reason,
                retry_detail_required,
                retry_execution_rejected,
            )
        )
        assert "hidden_payroll" not in json.dumps(schema)
        return SQLDraft(
            query_kind="detail"
            if retry_detail_required and self.detail_retry_sql
            else self.query_kind,
            sql=(
                self.retry_sql
                if retry_without_business_limit and self.retry_sql is not None
                else self.guard_retry_sql
                if retry_guard_reason and self.guard_retry_sql is not None
                else self.detail_retry_sql
                if retry_detail_required and self.detail_retry_sql is not None
                else self.execution_retry_sql
                if retry_execution_rejected and self.execution_retry_sql is not None
                else self.sql
            ),
            parameters=[]
            if self.query_kind == "aggregate" and not retry_detail_required
            else [BIND_VALUE],
            conditions_summary=MODEL_SUMMARY,
        )


class Transport:
    def __init__(self):
        self.calls = []
        self.after = None
        self.more = False
        self.delay_page = False
        self.reject_once = False

    async def execute_query(self, query, catalog, snapshot_id):
        self.calls.append((query, snapshot_id))
        if self.reject_once:
            self.reject_once = False
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        if self.delay_page and query.page_index > 0:
            await asyncio.sleep(30)
        if self.after:
            self.after()
        return QueryResult(
            columns=(ResultColumn("id", "bigint"), ResultColumn("code", "text")),
            rows=({"id": "9007199254740999", "code": ROW_VALUE},),
            row_count=1,
            has_more=self.more,
            referenced_relations=query.referenced_relations,
            elapsed_ms=1,
        )


class AnswerModel(Model):
    def __init__(self):
        self.page = 0
        self.delay = False
        self.inputs = []
        self.message_roles = []

    async def plan(self, messages, tools, feedback):
        assert "database_query" in {t.name for t in tools}
        return Plan(
            intent_kind="database",
            requires_enterprise_facts=True,
            tools=[
                {
                    "key": "database",
                    "tool_name": "database_query",
                    "arguments": {"question": "查询订单", "page_index": self.page},
                }
            ],
        )

    async def answer(self, messages):
        self.inputs.append(str(messages))
        self.message_roles.append([message.type for message in messages])
        assert ROW_VALUE in str(messages)
        assert BIND_VALUE not in str(messages)
        assert MODEL_SUMMARY not in str(messages)
        yield "已基于本轮授权业务数据完成查询。[[source_1]]\n"
        if self.delay:
            await asyncio.sleep(30)


@pytest.fixture
def query_agent(agent, database):
    source_id, snapshot_id = seed(database)
    client, headers, manager = agent
    generator, transport = QueryModel(), Transport()
    manager.database = DatabaseQueryService(manager.sessions, generator, transport)
    manager.model = AnswerModel()
    return agent, generator, transport, source_id, snapshot_id


def test_explicit_chart_query_binds_current_authorized_table_and_columns(query_agent, database):
    active, generator, _, source_id, snapshot_id = query_agent
    client, _, manager = active
    with Session(database.engine) as session:
        identity = CurrentIdentity(session.get(User, database.user_id()), None)
    question = (
        "查询 biz.orders，按 code 分组汇总 SUM(id)，"
        "维度列 code，指标列 total_amount，生成全部聚合结果的柱状图 PNG 预览。"
    )
    prepared = client.portal.call(
        manager.database.prepare, identity, DatabaseQueryInput(question=question), []
    )
    assert generator.selections == generator.generations == 0
    assert prepared.binding.database_source_id == source_id
    assert prepared.binding.schema_snapshot_id == snapshot_id
    assert [item.key for item in prepared.catalog.relations] == [("biz", "orders")]
    for invalid, expected in (
        (
            question.replace("biz.orders", "biz.hidden_payroll"),
            ErrorCode.DATABASE_RELATION_FORBIDDEN,
        ),
        (question.replace("SUM(id)", "SUM(secret_salary)"), ErrorCode.DATABASE_QUERY_REJECTED),
    ):
        with pytest.raises(APIError) as error:
            client.portal.call(
                manager.database.prepare, identity, DatabaseQueryInput(question=invalid), []
            )
        assert error.value.code == expected
    with Session(database.engine) as session, session.begin():
        policy = session.scalar(
            select(RelationPolicy).where(RelationPolicy.relation_name == "orders")
        )
        policy.enabled = False
        policy.row_version += 1
    with pytest.raises(APIError) as error:
        client.portal.call(
            manager.database.prepare, identity, DatabaseQueryInput(question=question), []
        )
    assert error.value.code == ErrorCode.DATABASE_RELATION_FORBIDDEN
    assert generator.selections == generator.generations == 0


def assert_no_private_data(database, caplog):
    needles = (BIND_VALUE, ROW_VALUE, MODEL_SUMMARY, SQL)
    with database.engine.connect() as connection:
        # Check every application row, not just a manually selected Tool JSON field.
        tables = connection.execute(
            text(
                "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN "
                "('iam','knowledge','agent','integration','infra','langgraph_checkpoint')"
            )
        ).all()
        for schema, table in tables:
            data = str(
                connection.execute(text(f'SELECT row_to_json(t) FROM "{schema}"."{table}" t')).all()
            )
            assert all(value not in data for value in needles), (schema, table)
        for table in ("checkpoint_blobs", "checkpoint_writes"):
            for blob in connection.execute(
                text(f"SELECT blob FROM langgraph_checkpoint.{table}")
            ).scalars():
                assert blob is None or all(value.encode() not in bytes(blob) for value in needles)
    assert all(value not in caplog.text for value in needles)


def test_database_csv_export_api_creates_durable_task_from_current_page(query_agent, database):
    active_agent, _, _, _, _ = query_agent
    client, headers, manager = active_agent
    conversation_id, run_id = new_run(active_agent, "请查询订单。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        source = session.scalar(select(AnswerSource))
        assert source is not None
        query_id, result_id = source.database_query_id, source.database_result_id
    assert query_id and result_id

    class PlanOwner:
        def __init__(self):
            self.plans = []

        async def register_export_plan(self, plan):
            self.plans.append(plan)

    owner = PlanOwner()
    client.app.state.database_client = owner
    client.app.state.storage = object()
    response = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/database-exports",
        {
            "scope": "current_page",
            "format": "csv",
            "query_id": query_id,
            "result_id": result_id,
            "file_name": "orders.csv",
        },
    )
    assert response.status_code == 202, response.text
    task_id = int(response.json()["data"]["task_id"])
    with Session(database.engine) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        plan = session.get(DatabaseExportPlan, task.export_plan_id)
        assert plan is not None
        assert (task.kind, task.format, task.status) == ("database_export", "csv", "queued")
        assert (plan.format, plan.scope, plan.query_id, plan.result_id) == (
            "csv",
            "current_page",
            query_id,
            result_id,
        )
        assert task.input_ref["file_name"] == "orders.csv"
        assert len(owner.plans) == 1
        assert owner.plans[0].handle == plan.runtime_handle


def test_cached_database_exports_create_tasks_without_optional_model_narration(
    query_agent, database
):
    from enterprise_mind.database.export_runtime import ExportRuntimePlanStore

    active_agent, generator, transport, _, _ = query_agent
    client, headers, manager = active_agent
    conversation_id, run_id = new_run(active_agent, "请查询订单。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"

    class PlanOwner:
        def __init__(self):
            self.plans = []

        async def register_export_plan(self, plan):
            self.plans.append(plan)

    class Worker:
        async def handle_event(self, event_id):
            return True

    owner = PlanOwner()
    manager.export_plans = ExportRuntimePlanStore()
    manager.export_plan_owner = owner
    manager.generation_worker = Worker()
    for scope, words in (
        ("current_page", "当前页"),
        ("all_matching", "全部符合条件的记录"),
        ("first_n", "前10条"),
    ):
        for file_format in ("csv", "xlsx"):
            response = post(
                client,
                headers,
                f"/api/v1/conversations/{conversation_id}/messages",
                {"content_text": f"请把刚才数据库查询的{words}导出为 {file_format.upper()}。"},
            )
            assert response.status_code == 202
            export_run_id = int(response.json()["data"]["id"])
            client.portal.call(manager.execute, export_run_id)
            final = snapshot(active_agent, export_run_id)
            assert final["status"] == "completed"
            with Session(database.engine) as session:
                message = session.get(Message, int(final["assistant_message_id"]))
                part = next(p for p in message.content_parts if p["type"] == "generated_file_task")
                task = session.get(GenerationTask, int(part["task_id"]))
                assert (task.kind, task.format, task.scope) == (
                    "database_export",
                    file_format,
                    scope,
                )
                assert words in message.content_text
    # AnswerModel.answer asserts that business rows are supplied. An export
    # from an already authorized context must not need that optional call.
    assert manager.model.inputs == []
    assert len(owner.plans) == 6
    assert generator.generations == 1
    assert len(transport.calls) == 1


def test_cached_first_n_export_requires_stable_original_order(query_agent, database):
    active_agent, generator, transport, _, _ = query_agent
    generator.sql = 'SELECT o.id, o.code FROM "biz"."orders" AS o WHERE o.code = $1'
    client, headers, manager = active_agent
    conversation_id, run_id = new_run(active_agent, "请查询订单。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    response = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请把刚才数据库查询的前10条导出为 CSV。"},
    )
    export_run_id = int(response.json()["data"]["id"])
    client.portal.call(manager.execute, export_run_id)
    final = snapshot(active_agent, export_run_id)
    assert final["status"] == "completed"
    with Session(database.engine) as session:
        message = session.get(Message, int(final["assistant_message_id"]))
        assert "稳定排序" in message.content_text
        assert not message.content_parts
        assert session.scalar(select(GenerationTask.id)) is None
    assert generator.generations == len(transport.calls) == 1
    assert manager.model.inputs == []


@pytest.mark.parametrize(
    "question",
    (
        "查询订单明细并分析其中的异常原因。",
        "Query order details and analyze the anomalies. Explain the reasons.",
    ),
)
def test_explicit_detail_analysis_receives_authorized_rows(query_agent, database, question):
    active_agent, _, _, _, _ = query_agent
    client, _, manager = active_agent
    _, run_id = new_run(active_agent, question)
    client.portal.call(manager.execute, run_id)
    final = snapshot(active_agent, run_id)
    assert final["status"] == "completed"
    assert len(manager.model.inputs) == 1
    assert ROW_VALUE in manager.model.inputs[0]
    with Session(database.engine) as session:
        message = session.get(Message, int(final["assistant_message_id"]))
        assert "本轮授权业务数据" in message.content_text


@pytest.mark.parametrize(
    "question_template",
    (
        "请把刚才的查询结果生成{scope}的{chart_type} PNG 预览。",
        "请将刚才的{scope}生成{chart_type} PNG。",
    ),
)
def test_cached_chart_scopes_do_not_require_optional_model_narration(
    query_agent, database, question_template
):
    from enterprise_mind.database.export_runtime import ExportRuntimePlanStore

    active, generator, transport, _, _ = query_agent
    generator.query_kind = "aggregate"
    generator.sql = (
        'SELECT SUM(o.id) AS id, o.code FROM "biz"."orders" AS o GROUP BY o.code ORDER BY o.code'
    )
    client, headers, manager = active
    conversation_id, run_id = new_run(active, "按订单 code 分组汇总 SUM(id)。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active, run_id)["status"] == "completed"
    manager.model.inputs.clear()

    class PlanOwner:
        async def register_export_plan(self, plan):
            pass

    class Worker:
        async def handle_event(self, event_id):
            return True

    manager.export_plans = ExportRuntimePlanStore()
    manager.export_plan_owner = PlanOwner()
    manager.generation_worker = Worker()
    for chart_type in ("柱状图", "折线图", "饼图"):
        for scope in ("当前页", "前1项", "全部聚合结果"):
            response = post(
                client,
                headers,
                f"/api/v1/conversations/{conversation_id}/messages",
                {"content_text": question_template.format(scope=scope, chart_type=chart_type)},
            )
            followup_id = int(response.json()["data"]["id"])
            client.portal.call(manager.execute, followup_id)
            final = snapshot(active, followup_id)
            assert final["status"] == "completed"
            with Session(database.engine) as session:
                message = session.get(Message, int(final["assistant_message_id"]))
                part = next(p for p in message.content_parts if p["type"] == "generated_file_task")
                task = session.get(GenerationTask, int(part["task_id"]))
                assert task.kind == "chart_render"
                assert scope in message.content_text
    assert manager.model.inputs == []
    assert generator.generations == len(transport.calls) == 1


def test_database_agent_sources_paging_and_continuous_question(query_agent, database, caplog):
    active_agent, generator, transport, source_id, snapshot_id = query_agent
    client, headers, manager = active_agent
    transport.more = True
    conversation_id, run_id = new_run(active_agent, "请查询订单。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        call = session.scalar(select(ToolCall).where(ToolCall.agent_run_id == run_id))
        assert set(call.arguments) == {
            "database_source_id",
            "schema_snapshot_id",
            "page_size",
            "page_index",
        }
        assert set(call.result_summary) == {"ok", "row_count", "has_more"}
        source = session.scalar(select(AnswerSource))
        assert source.database_source_id == source_id and source.schema_snapshot_id == snapshot_id
        assert source.source_type == "database" and source.has_more
        query_id = source.database_query_id
        result_id = source.database_result_id
        source_number = source.id
        answer = session.get(Message, source.message_id).content_text
        assert "并非完整全集" in answer
    preview = client.get(f"/api/v1/answer-sources/{source_number}/preview", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["data"]["locator"]["database_name"] == "synthetic_business"
    assert preview.json()["data"]["result_available"] is True
    assert preview.json()["data"]["can_continue"] is True
    assert preview.json()["data"]["returned_rows"] == 1
    assert preview.json()["data"]["columns"][0]["name"] == "id"
    assert preview.json()["data"]["rows"][0]["code"] == ROW_VALUE
    manager.model.page = 1
    result = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "继续下一页，沿用订单查询。"},
    )
    next_run = int(result.json()["data"]["id"])
    client.portal.call(manager.execute, next_run)
    assert snapshot(active_agent, next_run)["status"] == "completed"
    assert generator.generations == 1
    assert transport.calls[-1][0].page_index == 1
    assert len(manager.model.inputs) == 0
    assert any("请查询订单" in v for v in generator.semantics[-1])
    with Session(database.engine) as session:
        pages = list(session.scalars(select(AnswerSource).order_by(AnswerSource.id)).all())
        assert pages[-1].database_query_id == query_id
        assert pages[-1].database_result_id != result_id
        page_answer = session.get(Message, pages[-1].message_id).content_text
        assert "第 2 页已返回 1 行" in page_answer
        assert "[1]" in page_answer
    messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    ).json()["data"]
    database_sources = [
        source
        for message in messages
        for source in message["sources"]
        if source["source_type"] == "database"
    ]
    assert [source["can_continue"] for source in database_sources] == [False, True]
    assert_no_private_data(database, caplog)


def test_natural_language_continuation_delivers_235_rows_once(query_agent, database, caplog):
    """The Agent must keep one guarded query and deliver every page once."""

    active_agent, generator, _, _, _ = query_agent
    client, headers, manager = active_agent

    class PagingTransport(Transport):
        async def execute_query(self, query, catalog, snapshot_id):
            self.calls.append((query, snapshot_id))
            first = query.page_index * query.page_size + 1
            last = min(first + query.page_size - 1, 235)
            rows = tuple(
                {"id": str(identifier), "code": ROW_VALUE} for identifier in range(first, last + 1)
            )
            return QueryResult(
                columns=(ResultColumn("id", "bigint"), ResultColumn("code", "text")),
                rows=rows,
                row_count=len(rows),
                has_more=last < 235,
                referenced_relations=query.referenced_relations,
                elapsed_ms=1,
            )

    transport = PagingTransport()
    manager.database.transport = transport
    conversation_id, run_id = new_run(
        active_agent, "请查询全部订单明细，按 id 升序，每页返回 100 行。"
    )
    delivered_ids = []
    query_ids = []
    page_sizes = []
    has_more = []
    for index in range(3):
        client.portal.call(manager.execute, run_id)
        assert snapshot(active_agent, run_id)["status"] == "completed"
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["data"]
        current = next(
            message
            for message in messages
            if message["role"] == "assistant"
            and any(source["source_type"] == "database" for source in message["sources"])
            and message["sources"][0]["locator"]["page_index"] == index
        )
        source = current["sources"][0]
        preview = client.get(f"/api/v1/answer-sources/{source['id']}/preview", headers=headers)
        assert preview.status_code == 200
        data = preview.json()["data"]
        ids = [int(row["id"]) for row in data["rows"]]
        assert ids == list(range(index * 100 + 1, min((index + 1) * 100, 235) + 1))
        assert data["query_id"] == source["query_id"]
        assert data["referenced_relations"] == ['"biz"."orders"']
        assert data["queried_at"]
        assert f"[{1}]" in current["content_text"]
        assert f"has_more={'true' if data['has_more'] else 'false'}" in current["content_text"]
        assert "查询时间：" in current["content_text"]
        assert f"已返回 {data['returned_rows']} 行" in current["content_text"]
        delivered_ids.extend(ids)
        query_ids.append(source["query_id"])
        page_sizes.append(data["returned_rows"])
        has_more.append(data["has_more"])
        if index < 2:
            continuation = post(
                client,
                headers,
                f"/api/v1/conversations/{conversation_id}/messages",
                {"content_text": "继续下一页，沿用刚才的订单查询。"},
            )
            assert continuation.status_code == 202
            run_id = int(continuation.json()["data"]["id"])

    assert delivered_ids == list(range(1, 236))
    assert page_sizes == [100, 100, 35]
    assert has_more == [True, True, False]
    assert len(set(query_ids)) == 1
    assert [call[0].page_index for call in transport.calls] == [0, 1, 2]
    assert generator.generations == 1
    assert manager.model.inputs == []
    assert_no_private_data(database, caplog)


@pytest.mark.skipif(
    os.getenv("EM_TEST_LIVE_DATABASE_MODEL") != "1",
    reason="Set EM_TEST_LIVE_DATABASE_MODEL=1 for the real-model synthetic Agent chain",
)
def test_real_model_natural_language_continuation_235_rows(query_agent, database, caplog):
    """Optional real-Qwen Agent chain using an authorized synthetic transport."""

    active_agent, _, _, _, _ = query_agent
    client, headers, manager = active_agent
    config_path = Path(__file__).resolve().parents[3] / "config" / "providers.local.yaml"
    values = yaml.safe_load(config_path.read_text(encoding="utf-8"))["llm"]
    model = ChatModel(LLMConfig.model_validate(values))

    class PagingTransport(Transport):
        async def execute_query(self, query, catalog, snapshot_id):
            self.calls.append((query, snapshot_id))
            first = query.page_index * query.page_size + 1
            last = min(first + query.page_size - 1, 235)
            return QueryResult(
                columns=(ResultColumn("id", "bigint"), ResultColumn("code", "text")),
                rows=tuple(
                    {"id": str(identifier), "code": ROW_VALUE}
                    for identifier in range(first, last + 1)
                ),
                row_count=last - first + 1,
                has_more=last < 235,
                referenced_relations=query.referenced_relations,
                elapsed_ms=1,
            )

    transport = PagingTransport()
    manager.model = model
    manager.database.model = ChatDatabaseModel(model)
    manager.database.transport = transport
    try:
        conversation_id, run_id = new_run(
            active_agent,
            "请查询当前授权数据库 biz.orders 的全部订单明细，按 id 升序，每页返回 100 行。",
        )
        delivered = []
        query_ids = []
        has_more = []
        for index in range(3):
            client.portal.call(manager.execute, run_id)
            state = snapshot(active_agent, run_id)
            assert state["status"] == "completed", state.get("failure_code")
            messages = client.get(
                f"/api/v1/conversations/{conversation_id}/messages", headers=headers
            ).json()["data"]
            current = next(
                message
                for message in messages
                if message["role"] == "assistant"
                and any(
                    source["source_type"] == "database" and source["locator"]["page_index"] == index
                    for source in message["sources"]
                )
            )
            source = next(
                source for source in current["sources"] if source["source_type"] == "database"
            )
            preview = client.get(f"/api/v1/answer-sources/{source['id']}/preview", headers=headers)
            assert preview.status_code == 200
            page = preview.json()["data"]
            delivered.extend(int(row["id"]) for row in page["rows"])
            query_ids.append(page["query_id"])
            has_more.append(page["has_more"])
            assert page["queried_at"] and page["returned_rows"] == min(100, 235 - index * 100)
            if index < 2:
                response = post(
                    client,
                    headers,
                    f"/api/v1/conversations/{conversation_id}/messages",
                    {"content_text": "继续下一页，沿用刚才的订单查询。"},
                )
                assert response.status_code == 202
                run_id = int(response.json()["data"]["id"])
        assert delivered == list(range(1, 236))
        assert has_more == [True, True, False]
        assert len(set(query_ids)) == 1
        assert [call[0].page_index for call in transport.calls] == [0, 1, 2]
        assert_no_private_data(database, caplog)
    finally:
        client.portal.call(model.close)


def test_database_agent_removes_outer_limit_without_model_retry(query_agent, database, caplog):
    """A guarded outer row cap cannot truncate an unbounded user request."""

    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.sql = SQL + " LIMIT 50"
    _, run_id = new_run(active_agent, "请查询全部订单明细，每页返回 100 行。")
    client.portal.call(manager.execute, run_id)
    final = snapshot(active_agent, run_id)
    assert final["status"] == "completed"
    assert generator.generations == 1
    assert generator.correction_attempts == 0
    assert len(transport.calls) == 1
    assert transport.calls[0][0].has_business_limit is False
    assert_no_private_data(database, caplog)


def test_database_agent_corrects_unrequested_business_limit_once(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.sql = "SELECT id, code FROM (" + SQL + " LIMIT 50) AS selected ORDER BY id"
    generator.retry_sql = SQL
    question = "请查询全部订单明细，每页返回 100 行。"
    _, run_id = new_run(active_agent, question)

    client.portal.call(manager.execute, run_id)

    final = snapshot(active_agent, run_id)
    assert final["status"] == "completed"
    assert generator.generations == 2
    assert generator.correction_attempts == 1
    assert len(transport.calls) == 1
    query = transport.calls[0][0]
    assert query.has_business_limit is False
    assert query.page_size == 100 and query.page_index == 0
    first, correction = generator.generation_requests
    assert first[:3] == correction[:3]
    assert first[4] == correction[4]
    assert first[3] is False and correction[3] is True
    assert_no_private_data(database, caplog)


def test_database_agent_keeps_explicit_first_n_business_limit(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.sql = SQL + " LIMIT 1"
    _, run_id = new_run(active_agent, "请查询前 1 条订单明细。")
    client.portal.call(manager.execute, run_id)
    final = snapshot(active_agent, run_id)
    assert final["status"] == "completed"
    assert generator.generations == 1
    assert generator.correction_attempts == 0
    assert len(transport.calls) == 1
    assert_no_private_data(database, caplog)


def test_named_authorized_relation_repairs_one_guarded_sql_mistake(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.sql = 'SELECT id FROM "other"."orders" ORDER BY id'
    generator.guard_retry_sql = SQL
    _, run_id = new_run(active_agent, "请查询授权表 biz.orders 的订单明细，按 id 升序。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    assert generator.generations == 2
    assert generator.guard_retry_reasons == ["relation is not authorized"]
    assert len(transport.calls) == 1
    assert transport.calls[0][0].referenced_relations == ('"biz"."orders"',)
    assert_no_private_data(database, caplog)


def test_named_authorized_relation_repairs_one_invalid_sql_text(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.sql = "SELECT FROM"
    generator.guard_retry_sql = SQL
    _, run_id = new_run(active_agent, "请查询授权表 biz.orders 的订单明细，按 id 升序。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    assert generator.generations == 2
    assert generator.guard_retry_reasons == ["invalid text"]
    assert len(transport.calls) == 1
    assert_no_private_data(database, caplog)


def test_explicit_detail_request_repairs_aggregate_once(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.sql = 'SELECT count(*) FROM "biz"."orders"'
    generator.query_kind = "aggregate"
    generator.detail_retry_sql = SQL
    _, run_id = new_run(active_agent, "请查询 biz.orders 的全部订单明细，按 id 升序。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    assert generator.generations == 2 and generator.detail_retry_count == 1
    assert len(transport.calls) == 1
    assert transport.calls[0][0].referenced_relations == ('"biz"."orders"',)
    assert_no_private_data(database, caplog)


def test_named_relation_repairs_one_execution_rejection(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    generator.execution_retry_sql = SQL
    transport.reject_once = True
    _, run_id = new_run(active_agent, "请查询 biz.orders 的订单明细，按 id 升序。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    assert generator.generations == 2 and generator.execution_retry_count == 1
    assert len(transport.calls) == 2
    assert all(call[0].referenced_relations == ('"biz"."orders"',) for call in transport.calls)
    assert_no_private_data(database, caplog)


def test_database_query_restores_from_checkpoint_by_regenerating(query_agent, database, caplog):
    active_agent, generator, transport, _, _ = query_agent
    client, headers, manager = active_agent
    generator.sql = 'SELECT count(*) FROM "biz"."orders"'
    generator.query_kind = "aggregate"
    manager.model.delay = True
    _, run_id = new_run(active_agent, "查询订单并提供来源。")
    future = client.portal.start_task_soon(manager.execute, run_id)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and not manager.model.inputs:
        time.sleep(0.05)
    assert manager.model.inputs
    assert client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=headers).status_code == 200
    future.result(timeout=10)
    assert snapshot(active_agent, run_id)["status"] == "cancelled"
    assert_no_private_data(database, caplog)
    manager.model.delay = False
    assert client.post(f"/api/v1/agent-runs/{run_id}/resume", headers=headers).status_code == 202
    client.portal.call(manager.execute, run_id)
    final = snapshot(active_agent, run_id)
    assert final["status"] == "completed"
    assert (final["tool_call_count"], final["retry_count"], final["replan_count"]) == (1, 0, 0)
    assert generator.generations == len(transport.calls) == 2
    assert_no_private_data(database, caplog)


def test_database_continuation_checkpoint_expires_without_process_local_binding(
    query_agent, database, caplog
):
    active_agent, generator, transport, _, _ = query_agent
    client, headers, manager = active_agent
    transport.more = True
    conversation_id, first_run = new_run(active_agent, "请查询订单。")
    client.portal.call(manager.execute, first_run)
    assert snapshot(active_agent, first_run)["status"] == "completed"
    with Session(database.engine) as session:
        source = session.scalar(select(AnswerSource))
        assert source is not None
        query_id, result_id = source.database_query_id, source.database_result_id
    assert query_id and result_id
    assert generator.generations == 1 and len(transport.calls) == 1

    continuation = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/database-queries/{query_id}/pages",
        {"query_id": query_id, "result_id": result_id},
    )
    assert continuation.status_code == 202, continuation.text
    continuation_run = int(continuation.json()["data"]["id"])
    transport.delay_page = True
    future = client.portal.start_task_soon(manager.execute, continuation_run)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and len(transport.calls) < 2:
        time.sleep(0.05)
    assert len(transport.calls) == 2
    assert (
        client.post(f"/api/v1/agent-runs/{continuation_run}/stop", headers=headers).status_code
        == 200
    )
    future.result(timeout=10)
    transport.delay_page = False
    assert snapshot(active_agent, continuation_run)["status"] == "cancelled"
    assert generator.generations == 1 and len(transport.calls) == 2

    # Simulate process replacement: neither the guarded definition nor its
    # current result page is durable.  Recovery must fail closed without
    # regenerating SQL or touching the transport.
    manager.database_queries._items.clear()
    manager.database_queries._latest.clear()
    manager.database_pages._items.clear()
    manager.database_pages._query_results.clear()
    assert (
        client.post(f"/api/v1/agent-runs/{continuation_run}/resume", headers=headers).status_code
        == 202
    )
    client.portal.call(manager.execute, continuation_run)
    final = snapshot(active_agent, continuation_run)
    assert final["status"] == "failed"
    assert final["failure_code"] == "QUERY_CONTEXT_EXPIRED"
    assert generator.generations == 1 and len(transport.calls) == 2
    assert_no_private_data(database, caplog)


def test_critical_query_ambiguity_uses_existing_pending_action(query_agent, database):
    active_agent, generator, transport, _, _ = query_agent
    client, headers, manager = active_agent
    generator.ambiguous = True
    _, run_id = new_run(active_agent, "查询近期订单。")
    client.portal.call(manager.execute, run_id)
    state = snapshot(active_agent, run_id)
    assert state["status"] == "waiting_confirmation"
    assert generator.generations == 0 and transport.calls == []
    action_id = state["pending_action_id"]
    pending = client.get(f"/api/v1/pending-actions/{action_id}", headers=headers).json()["data"]
    assert pending["action_type"] == "disambiguation"
    accepted = post(
        client,
        headers,
        f"/api/v1/pending-actions/{action_id}/responses",
        {"decision": "accept", "selected_target_id": "1"},
    )
    assert accepted.status_code == 200, accepted.text
    client.portal.call(manager.execute, run_id)
    assert snapshot(active_agent, run_id)["status"] == "completed"
    assert len(transport.calls) == 1
    with Session(database.engine) as session:
        assert session.get(PendingAction, int(action_id)).status == "accepted"


@pytest.mark.parametrize("attack", ["write", "revoke"])
def test_database_rejected_or_revoked_results_never_reach_answer(
    query_agent, database, caplog, attack
):
    active_agent, generator, transport, _, _ = query_agent
    client, _, manager = active_agent
    if attack == "write":
        generator.sql = 'DELETE FROM "biz"."orders" WHERE code=$1 RETURNING id'
    else:

        def revoke():
            with Session(database.engine) as session, session.begin():
                policy = session.scalar(
                    select(RelationPolicy).where(RelationPolicy.relation_name == "orders")
                )
                policy.enabled = False

        transport.after = revoke
    _, run_id = new_run(active_agent, "查询订单。")
    client.portal.call(manager.execute, run_id)
    final = snapshot(active_agent, run_id)
    assert final["status"] == "failed"
    assert final["failure_code"] == (
        "DATABASE_QUERY_REJECTED" if attack == "write" else "DATABASE_RELATION_FORBIDDEN"
    )
    assert generator.generations == 1 and manager.model.inputs == []
    assert final["retry_count"] == final["replan_count"] == 0
    if attack == "write":
        assert transport.calls == []
    assert_no_private_data(database, caplog)


def test_database_tool_rejects_raw_sql_and_control_injection():
    from pydantic import ValidationError

    for field in ("sql", "parameters", "schema_snapshot_id", "actor_user_id"):
        with pytest.raises(ValidationError):
            DatabaseQueryInput.model_validate({"question": "查询订单", field: "bad"})


def test_database_query_redis_and_sse_never_carry_private_payload(
    query_agent, database, ingestion_settings, caplog
):
    active_agent, _, _, _, _ = query_agent
    client, headers, manager = active_agent
    settings, _, redis_admin = ingestion_settings
    queue = TaskQueue(settings)
    recorded = []

    class RecordingBus(EventBus):
        async def publish(self, run_id, event, data):
            recorded.append((event, data))
            await super().publish(run_id, event, data)

    manager.bus = RecordingBus(queue.redis, settings.redis_stream + ":database-agent")
    try:
        _, run_id = new_run(active_agent, "查询订单并提供来源。")
        client.portal.call(manager.execute, run_id)
        assert snapshot(active_agent, run_id)["status"] == "completed"
        response = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers)
        assert response.status_code == 200
        assert "run.completed" in response.text
        assert recorded
        for marker in (BIND_VALUE, ROW_VALUE, MODEL_SUMMARY, SQL):
            assert marker not in str(recorded) + response.text
        # Real Redis Pub/Sub does not create durable keys or query-result streams.
        assert list(redis_admin.scan_iter(match=settings.redis_stream + "*")) == []
        assert_no_private_data(database, caplog)
    finally:
        client.portal.call(queue.close)
