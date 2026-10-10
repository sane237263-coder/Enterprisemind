import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent import Model, new_run, snapshot
from test_management import auth
from test_rag_pipeline import indexed, ingestion_runtime, upload_index  # noqa: F401

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.agent.events import EventBus
from enterprise_mind.agent.models import AnswerSource, Message, ToolCall
from enterprise_mind.agent.runner import Execution
from enterprise_mind.agent.schemas import Plan, ProposedTool
from enterprise_mind.app import create_app
from enterprise_mind.ingestion.models import Document
from enterprise_mind.rag.models import Chunk
from enterprise_mind.rag.providers import ProviderFailure
from enterprise_mind.rag.repository import RetrievalRepository
from enterprise_mind.rag.retrieval import RetrievalService

pytestmark = pytest.mark.integration


def test_redis_transient_events_cross_instances_and_isolate_run_channels(runtime):
    async def check():
        source = runtime.client.app.state.agent.bus
        receiver = EventBus(source.redis, source.prefix)
        async with receiver.subscribe(101) as read, receiver.subscribe(102) as other:
            # Publishing before the first read must not lose the opening delta.
            await source.publish(101, "answer.delta", {"text": "本轮增量"})
            async with asyncio.timeout(2):
                value = await read(1)
                while value is None:
                    value = await read(1)
            assert value == {"event": "answer.delta", "data": {"text": "本轮增量"}}
            assert await other(0.05) is None

    runtime.run(check)


class KnowledgeModel(Model):
    async def plan(self, messages, tools, feedback):
        self.plan_calls += 1
        return Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            tools=[
                ProposedTool(
                    key="a", tool_name="knowledge_retrieve", arguments={"question": "ABC-123"}
                ),
                ProposedTool(
                    key="b", tool_name="knowledge_retrieve", arguments={"question": "企业加密"}
                ),
                ProposedTool(
                    key="c",
                    tool_name="knowledge_retrieve",
                    arguments={"question": "ABC-123"},
                    depends_on=["a", "b"],
                ),
            ],
        )

    async def answer(self, messages):
        yield "当前资料说明支持企业加密。" * 12 + "["
        yield "[source_"
        yield "1]]。"


def test_short_deltas_preserve_split_citation_markers(rag_agent, database, monkeypatch):
    client, _, manager = rag_agent
    emitted = []
    publish = manager.bus.publish

    async def capture(run_id, event, data):
        if event == "answer.delta":
            emitted.append(data["text"])
        await publish(run_id, event, data)

    async def answer(messages):
        yield "企业加密"
        assert emitted == ["企业加密"]
        for part in ("[", "[source_", "1", "]"):
            yield part
            assert emitted == ["企业加密"]
        yield "]"
        assert emitted == ["企业加密", "[1]"]
        yield "。"

    monkeypatch.setattr(manager.bus, "publish", capture)
    monkeypatch.setattr(manager.model, "answer", answer)
    _, run_id = new_run(rag_agent, "查询企业加密")
    client.portal.call(manager.execute, run_id)
    assert snapshot(rag_agent, run_id)["status"] == "completed"
    assert emitted == ["企业加密", "[1]", "。"]
    with Session(database.engine) as session:
        message = session.get(Message, int(snapshot(rag_agent, run_id)["assistant_message_id"]))
        assert message.content_text == "企业加密[1]。"


@pytest.fixture
def rag_agent(indexed, database, ingestion_settings):  # noqa: F811
    runtime, retrieval, *_ = indexed
    upload_index(indexed)
    with Session(database.engine) as session:
        document = session.scalars(select(Document)).one()
        assert document.processing_status == "success"
    app = create_app(database.settings, ingestion_settings[0])
    manager = app.state.agent
    manager.start = lambda: None
    manager.model = KnowledgeModel()
    manager.retrieval = RetrievalService(
        RetrievalRepository(
            app.state.sessions, retrieval.config, indexed[3].model_key, indexed[3].dimensions
        ),
        indexed[3],
        indexed[4],
        retrieval.config,
    )
    manager.config.poll_interval_seconds = 0.05
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        client.portal.call(initialize_checkpoints, database.url)
        yield client, auth(client, "初始管理员", database.admin_password), manager


def test_actual_rag_parallel_dependency_waves_and_persisted_source(rag_agent, database):
    client, headers, manager = rag_agent
    active, peak = 0, 0
    retrieve = manager.retrieval.retrieve

    async def observed(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        try:
            return await retrieve(*args, **kwargs)
        finally:
            active -= 1

    manager.retrieval.retrieve = observed
    conversation, run = new_run(rag_agent, "ABC-123 是否支持加密？")
    client.portal.call(manager.execute, run)
    state = snapshot(rag_agent, run)
    assert state["status"] == "completed", state
    assert state["tool_call_count"] == 3 and peak == 2
    with Session(database.engine) as session:
        calls = session.scalars(select(ToolCall).order_by(ToolCall.id)).all()
        assert calls[2].started_at >= max(calls[0].finished_at, calls[1].finished_at)
        sources = session.scalars(select(AnswerSource)).all()
        assert len(sources) == 1 and sources[0].quote_text.startswith("ABC-123"), [
            (call.status, call.result_summary, call.error_code) for call in calls
        ]
        source_id = sources[0].id
        answer = session.get(Message, int(state["assistant_message_id"]))
        assert "[1]" in answer.content_text and "source_1" not in answer.content_text
        assert all(set(call.result_summary) <= {"status", "chunk_ids", "limited"} for call in calls)
    output = client.get(f"/api/v1/answer-sources/{source_id}/preview", headers=headers).json()[
        "data"
    ]
    assert output["available"]
    assert output["knowledge_base_name"] == "测试知识库"
    assert output["document_name"] == "manual.txt"
    with Session(database.engine) as session, session.begin():
        # Revoke source visibility without changing the conversation owner's history.
        from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase

        base = session.scalars(select(KnowledgeBase)).one()
        now = datetime.now(UTC)
        batch = DeletionBatch(
            root_type="knowledge_base",
            root_id=base.id,
            root_name=base.name,
            deleted_at=now,
            purge_after=now + timedelta(days=30),
        )
        session.add(batch)
        session.flush()
        base.deleted_at, base.purge_after, base.deletion_batch_id = now, batch.purge_after, batch.id
    output = client.get(f"/api/v1/answer-sources/{source_id}/preview", headers=headers)
    assert output.status_code == 404 and output.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
    messages = client.get(f"/api/v1/conversations/{conversation}/messages", headers=headers).json()
    assert messages["data"][-1]["sources"] == [
        {"id": str(source_id), "ordinal": 1, "source_type": "knowledge", "available": False}
    ]


@pytest.mark.parametrize("text", ["编造的来源[[source_999]]。", "沿用历史编号 [1]。"])
def test_invalid_citation_never_commits_a_successful_message(rag_agent, database, text):
    client, _, manager = rag_agent

    async def invalid(messages):
        assert messages[0].type == "system"
        assert messages[-1].type == "human"
        assert "[[source_1]]" in messages[0].content
        assert "历史回答和原文中的 [1]" in messages[0].content
        yield text

    manager.model.answer = invalid
    _, run = new_run(rag_agent, "ABC-123")
    client.portal.call(manager.execute, run)
    assert snapshot(rag_agent, run)["status"] == "failed"
    assert snapshot(rag_agent, run)["failure_code"] == "ANSWER_SOURCE_INVALID"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(AnswerSource)) == 0


def test_source_withdrawal_before_commit_never_persists_completed_answer(
    rag_agent, database, monkeypatch
):
    client, _, manager = rag_agent
    original = Execution.persist_terminal

    async def withdraw(self, state):
        with Session(database.engine) as session, session.begin():
            session.scalars(select(Document)).one().active_revision_id = None
        return await original(self, state)

    monkeypatch.setattr(Execution, "persist_terminal", withdraw)
    _, run_id = new_run(rag_agent, "ABC-123")
    client.portal.call(manager.execute, run_id)
    state = snapshot(rag_agent, run_id)
    assert state["status"] == "failed"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(AnswerSource)) == 0
        assert session.get(Message, int(state["assistant_message_id"])).status == "failed"


def test_partial_branch_failure_does_not_block_other_dependencies(rag_agent, database):
    client, _, manager = rag_agent
    manager.config.tool_max_retries = 0
    retrieve = manager.retrieval.retrieve

    async def partial(identity, question, **kwargs):
        if question == "企业加密":
            raise ProviderFailure("PRIVATE_ERROR_BODY")
        return await retrieve(identity, question, **kwargs)

    async def plan(messages, tools, feedback):
        return Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            tools=[
                ProposedTool(
                    key="a", tool_name="knowledge_retrieve", arguments={"question": "ABC-123"}
                ),
                ProposedTool(
                    key="b", tool_name="knowledge_retrieve", arguments={"question": "企业加密"}
                ),
                ProposedTool(
                    key="c",
                    tool_name="knowledge_retrieve",
                    arguments={"question": "ABC-123"},
                    depends_on=["a"],
                ),
                ProposedTool(
                    key="d",
                    tool_name="knowledge_retrieve",
                    arguments={"question": "ABC-123"},
                    depends_on=["b"],
                ),
            ],
        )

    manager.retrieval.retrieve = partial
    manager.model.plan = plan
    _, run_id = new_run(rag_agent, "ABC-123")
    client.portal.call(manager.execute, run_id)
    state = snapshot(rag_agent, run_id)
    assert state["status"] == "completed"
    with Session(database.engine) as session:
        calls = session.scalars(select(ToolCall).order_by(ToolCall.id)).all()
        assert [call.status for call in calls] == ["success", "failed", "success", "cancelled"]
        answer = session.get(Message, int(state["assistant_message_id"]))
        assert "部分检索" in answer.content_text and "PRIVATE_ERROR_BODY" not in answer.content_text


def test_document_summary_discloses_partial_chunk_coverage(rag_agent, indexed, database):  # noqa: F811
    client, _, manager = rag_agent
    body = "\n\n".join(
        f"## 条目 {index}\nABC-123 风险描述。" + "本条仅用于测试总结覆盖范围。" * 30
        for index in range(50)
    )
    document_id = int(upload_index(indexed, name="long-summary.md", body=body)["document_id"])
    with Session(database.engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
            )
            > 10
        )

    async def plan(messages, tools, feedback):
        return Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            tools=[
                ProposedTool(
                    key="read",
                    tool_name="knowledge_retrieve",
                    arguments={"question": "ABC-123 风险", "document_names": ["long-summary.md"]},
                )
            ],
        )

    manager.model.plan = plan
    _, run_id = new_run(rag_agent, "总结文档 long-summary.md 的风险条目")
    client.portal.call(manager.execute, run_id)
    state = snapshot(rag_agent, run_id)
    assert state["status"] == "completed", state
    with Session(database.engine) as session:
        answer = session.get(Message, int(state["assistant_message_id"]))
        assert answer is not None and "仅覆盖本轮检索到的文档片段" in answer.content_text
        source_document_ids = set(
            session.scalars(
                select(Chunk.document_id)
                .join(AnswerSource, AnswerSource.chunk_id == Chunk.id)
                .where(AnswerSource.message_id == answer.id)
            )
        )
        assert source_document_ids == {document_id}


def test_provider_failure_counts_retries_and_replans_separately(rag_agent):
    client, _, manager = rag_agent
    manager.config.tool_max_retries = 1
    manager.config.max_replans = 1
    attempts = 0

    async def unavailable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ProviderFailure("TEST_FAILURE")

    manager.retrieval.retrieve = unavailable
    _, run = new_run(rag_agent, "ABC-123")
    client.portal.call(manager.execute, run)
    state = snapshot(rag_agent, run)
    assert state["status"] == "failed" and state["failure_code"] == "RUN_LIMIT_REACHED"
    assert state["replan_count"] == 1 and state["retry_count"] == 4
    assert state["tool_call_count"] == 6 and attempts == 8


def test_explicit_empty_scope_cannot_expand(rag_agent):
    client, _, manager = rag_agent

    async def plan(messages, tools, feedback):
        return Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            tools=[
                ProposedTool(
                    key="empty",
                    tool_name="knowledge_retrieve",
                    arguments={"question": "ABC-123", "document_ids": []},
                ),
            ],
        )

    manager.model.plan = plan
    conversation, run = new_run(rag_agent, "ABC-123")
    client.portal.call(manager.execute, run)
    assert snapshot(rag_agent, run)["status"] == "completed"
    result = client.get(
        f"/api/v1/conversations/{conversation}/messages", headers=rag_agent[1]
    ).json()["data"]
    assert "没有找到足够依据" in result[-1]["content_text"]
    assert result[-1]["sources"] == []
