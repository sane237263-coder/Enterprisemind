import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_management import auth

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.agent.models import (
    AgentRun,
    Conversation,
    ConversationSummary,
    Message,
    ToolCall,
)
from enterprise_mind.agent.runner import Execution
from enterprise_mind.agent.schemas import Plan
from enterprise_mind.app import create_app
from enterprise_mind.identity.models import User
from enterprise_mind.knowledge.models import DeletionBatch, KnowledgeBase
from enterprise_mind.knowledge.pending_models import PendingAction
from enterprise_mind.rag.providers import ProviderFailure

pytestmark = pytest.mark.integration


class Counter:
    def count(self, value):
        return len(value)


class Model:
    counter = Counter()
    max_input_tokens = 32000
    max_output_tokens = 2000
    delay = False
    plan_calls = 0

    async def plan(self, messages, tools, feedback):
        self.plan_calls += 1
        return Plan(intent_kind="general", requires_enterprise_facts=False)

    async def answer(self, messages):
        yield "这里是已经生成的回答片段。\n"
        if self.delay:
            await asyncio.sleep(30)
        yield "完整回答。"

    async def summarize(self, messages):
        return "用户在讨论一个持续任务。"

    async def close(self):
        pass


@pytest.fixture
def agent(database):
    database.initialize()
    app = create_app(database.settings)
    app.state.agent.start = lambda: None
    app.state.agent.model = Model()
    app.state.agent.config.poll_interval_seconds = 0.05
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        client.portal.call(initialize_checkpoints, database.url)
        headers = auth(client, "初始管理员", database.admin_password)
        yield client, headers, app.state.agent


def post(client, headers, path, body, key=None):
    return client.post(
        path, headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex}, json=body
    )


def new_run(agent, content="请写一段欢迎词。", key=None):
    client, headers, _ = agent
    result = post(client, headers, "/api/v1/conversations", {"title": "会话"})
    assert result.status_code == 201, result.text
    conversation_id = result.json()["data"]["id"]
    result = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": content},
        key,
    )
    assert result.status_code == 202, result.text
    return conversation_id, int(result.json()["data"]["id"])


def create_public_knowledge_base(client, headers, name):
    result = post(
        client,
        headers,
        "/api/v1/knowledge-bases",
        {"name": name, "type": "public"},
    )
    assert result.status_code == 201, result.text
    return result.json()["data"]


def seed_long_history(database, conversation_id):
    with Session(database.engine) as session, session.begin():
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        for sequence in range(1, 9):
            session.add(
                Message(
                    conversation_id=conversation_id,
                    sequence=sequence,
                    role="user" if sequence % 2 else "assistant",
                    status="completed",
                    content_text="旧历史中的讨论内容，用于触发摘要。" * 8,
                )
            )
        conversation.next_message_sequence = 9


def test_conversation_latest_run_is_owner_scoped_and_restores_history(agent):
    client, headers, manager = agent
    conversation_id, first_run = new_run(agent)
    path = f"/api/v1/conversations/{conversation_id}"
    assert client.get(path, headers=headers).json()["data"]["latest_run_id"] == str(first_run)
    assert client.portal.call(manager.execute, first_run)
    second = post(client, headers, path + "/messages", {"content_text": "继续下一轮"})
    assert second.status_code == 202
    assert (
        client.get(path, headers=headers).json()["data"]["latest_run_id"]
        == second.json()["data"]["id"]
    )
    result = post(
        client,
        headers,
        "/api/v1/admin/users",
        {
            "username": "其他会话所有者",
            "password": "Test-Other-Owner-2026!",
            "role": "admin",
        },
    )
    assert result.status_code == 201
    other = auth(client, "其他会话所有者", "Test-Other-Owner-2026!")
    denied = client.get(path, headers=other)
    assert denied.status_code == 404
    assert "latest_run_id" not in denied.text


@pytest.mark.parametrize("model_name", ["fixture/model-v1", "fixture/model-v2", None])
def test_model_identity_uses_runtime_config_without_any_model_call(
    agent, database, monkeypatch, model_name
):
    client, headers, manager = agent
    conversation_id, prior_run = new_run(agent)
    client.portal.call(manager.execute, prior_run)
    manager.config.recent_message_count = 1
    manager.config.summary_threshold_tokens = 1
    monkeypatch.setattr(manager.model, "model_name", model_name, raising=False)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Model identity must not invoke the upstream provider")

    for method in ("plan", "answer", "summarize"):
        monkeypatch.setattr(manager.model, method, forbidden)
    monkeypatch.setattr(manager, "schedule_memory_extraction", forbidden)
    submitted = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "你背后的模型是什么？"},
    )
    assert submitted.status_code == 202
    run_id = int(submitted.json()["data"]["id"])
    assert client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "completed" and state["tool_call_count"] == 0
    with Session(database.engine) as session:
        answer = session.get(Message, int(state["assistant_message_id"]))
        assert answer.status == "completed"
        assert "EnterpriseMind" in answer.content_text
        if model_name:
            assert model_name in answer.content_text
        else:
            assert "无法确认" in answer.content_text
        assert session.scalar(select(func.count()).select_from(ConversationSummary)) == 0


@pytest.mark.parametrize(
    ("phase", "failure_code", "message_fragment"),
    [
        ("plan", "MODEL_PROVIDER_RATE_LIMITED", "稍后重试"),
        ("plan", "MODEL_PLANNING_TIMEOUT", "规划超过时间限制"),
        ("answer", "MODEL_PROVIDER_RATE_LIMITED", "稍后重试"),
    ],
)
def test_provider_failure_survives_run_snapshot_and_sse(
    agent, database, phase, failure_code, message_fragment
):
    client, headers, manager = agent

    class LimitedModel(Model):
        async def plan(self, messages, tools, feedback):
            if phase == "plan":
                raise ProviderFailure(failure_code)
            return await super().plan(messages, tools, feedback)

        async def answer(self, messages):
            yield "未完成的片段。\n"
            raise ProviderFailure(failure_code)

    manager.model = LimitedModel()
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "failed"
    assert state["failure_code"] == failure_code
    assert message_fragment in state["failure_message"]
    events = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers)
    assert "event: run.failed" in events.text
    assert failure_code in events.text
    assert "event: run.completed" not in events.text
    with Session(database.engine) as session:
        run = session.get(AgentRun, run_id)
        if run.assistant_message_id:
            assert session.get(Message, run.assistant_message_id).status != "completed"
        assert session.scalar(text("SELECT count(*) FROM langgraph_checkpoint.checkpoints")) == 0


def test_direct_knowledge_catalog_skips_model_and_memory_and_matches_authorized_api(
    agent, database, monkeypatch
):
    client, headers, manager = agent
    bases = [
        create_public_knowledge_base(client, headers, "目录核验公共库甲"),
        create_public_knowledge_base(client, headers, "目录核验公共库乙"),
    ]
    api_page = client.get("/api/v1/knowledge-bases", headers=headers, params={"limit": 50})
    assert api_page.status_code == 200, api_page.text
    metadata = api_page.json()["data"]
    assert api_page.json()["meta"]["next_cursor"] is None
    assert {item["id"] for item in bases}.issubset({item["id"] for item in metadata})

    conversation_result = post(client, headers, "/api/v1/conversations", {"title": "知识库目录"})
    conversation_id = int(conversation_result.json()["data"]["id"])
    seed_long_history(database, conversation_id)
    manager.config.summary_threshold_tokens = 1
    manager.config.recent_message_count = 1

    calls = []

    class ForbiddenModel(Model):
        async def plan(self, messages, tools, feedback):
            calls.append("plan")
            raise AssertionError("the direct catalog request must bypass planning")

        async def summarize(self, messages):
            calls.append("summarize")
            raise AssertionError("the direct catalog request must bypass summarization")

        async def answer(self, messages):
            calls.append("answer")
            raise AssertionError("the direct catalog request must bypass answer generation")
            yield "unreachable"

    manager.model = ForbiddenModel()

    async def forbidden_memory(*args, **kwargs):
        calls.append("memory")
        raise AssertionError("the direct catalog request must skip automatic Memory extraction")

    monkeypatch.setattr(manager, "schedule_memory_extraction", forbidden_memory)
    submitted = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "我当前有权限的知识库有哪些？"},
    )
    assert submitted.status_code == 202, submitted.text
    run_id = int(submitted.json()["data"]["id"])
    assert client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "completed" and state["tool_call_count"] == 1
    assert calls == []

    expected_lines = [f"当前有权限访问的知识库（本页 {len(metadata)} 个）："]
    for item in metadata:
        label = "企业公共库" if item["type"] == "public" else "部门库"
        expected_lines.append(f"- {item['name']}（{label}，ID：{item['id']}）")
    expected_lines.append("本页之后没有更多知识库记录。")

    with Session(database.engine) as session:
        run = session.get(AgentRun, run_id)
        message = session.get(Message, run.assistant_message_id)
        tool_calls = list(session.scalars(select(ToolCall).where(ToolCall.agent_run_id == run_id)))
        assert message.status == "completed" and message.content_parts is None
        assert message.content_text == "\n".join(expected_lines)
        assert len(tool_calls) == 1
        call = tool_calls[0]
        assert call.tool_name == "knowledge_base_list" and call.status == "success"
        assert call.risk_level == "low"
        assert call.result_summary == {
            "ids": [item["id"] for item in metadata],
            "count": len(metadata),
            "has_more": False,
            "next_after_id": None,
        }
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 0
        assert session.scalar(select(func.count()).select_from(ConversationSummary)) == 0


def test_model_proposed_catalog_keeps_names_out_of_future_model_history(agent, database):
    client, headers, manager = agent
    name = "目录答案不得进入模型上下文-唯一标记"
    create_public_knowledge_base(client, headers, name)
    conversation_result = post(client, headers, "/api/v1/conversations", {"title": "目录追问"})
    conversation_id = int(conversation_result.json()["data"]["id"])
    seed_long_history(database, conversation_id)
    manager.config.summary_threshold_tokens = 1
    manager.config.recent_message_count = 1

    class CatalogAwareModel(Model):
        def __init__(self):
            self.plan_calls = 0
            self.plan_inputs = []
            self.plan_tools = []
            self.summary_inputs = []
            self.answer_inputs = []

        async def plan(self, messages, tools, feedback):
            self.plan_calls += 1
            self.plan_inputs.append(str(messages))
            self.plan_tools.append(
                str([(tool.name, tool.description, tool.args_schema) for tool in tools])
            )
            if "资料库" in str(messages[-1].content):
                assert "knowledge_base_list" in {tool.name for tool in tools}
                return Plan(
                    intent_kind="knowledge",
                    requires_enterprise_facts=True,
                    tools=[
                        {
                            "key": "catalog",
                            "tool_name": "knowledge_base_list",
                            "arguments": {},
                        }
                    ],
                )
            return Plan(intent_kind="general", requires_enterprise_facts=False)

        async def summarize(self, messages):
            self.summary_inputs.append(str(messages))
            return "用户持续讨论一般话题。"

        async def answer(self, messages):
            self.answer_inputs.append(str(messages))
            yield "普通欢迎语。"

    model = CatalogAwareModel()
    manager.model = model
    submitted = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "我有哪些可用的资料库？"},
    )
    assert submitted.status_code == 202, submitted.text
    catalog_run_id = int(submitted.json()["data"]["id"])
    assert client.portal.call(manager.execute, catalog_run_id)
    catalog_state = snapshot(agent, catalog_run_id)
    assert catalog_state["status"] == "completed" and catalog_state["tool_call_count"] == 1
    assert model.plan_calls == 1
    assert "knowledge_base_list" in model.plan_tools[0]
    assert not model.answer_inputs
    assert (
        name
        in client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers).json()[
            "data"
        ][-1]["content_text"]
    )
    with Session(database.engine) as session:
        catalog_calls = list(
            session.scalars(select(ToolCall).where(ToolCall.agent_run_id == catalog_run_id))
        )
        assert len(catalog_calls) == 1
        assert catalog_calls[0].tool_name == "knowledge_base_list"
        assert catalog_calls[0].status == "success"
        assert name not in str(catalog_calls[0].result_summary)
    summaries_before_followup = len(model.summary_inputs)

    followup = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请写一句普通欢迎语。"},
    )
    assert followup.status_code == 202, followup.text
    followup_run_id = int(followup.json()["data"]["id"])
    assert client.portal.call(manager.execute, followup_run_id)
    followup_state = snapshot(agent, followup_run_id)
    assert followup_state["status"] == "completed" and followup_state["tool_call_count"] == 0
    assert model.plan_calls == 2 and len(model.summary_inputs) > summaries_before_followup
    assert model.answer_inputs
    assert all(
        name not in observed
        for observed in [
            *model.plan_inputs,
            *model.plan_tools,
            *model.summary_inputs,
            *model.answer_inputs,
        ]
    )

    with Session(database.engine) as session:
        summaries = list(
            session.scalars(
                select(ConversationSummary).where(
                    ConversationSummary.conversation_id == conversation_id
                )
            )
        )
        assert summaries and all(name not in item.summary_text for item in summaries)
        messages = list(
            session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence)
            )
        )
        catalog_message = next(item for item in messages if name in (item.content_text or ""))
        assert catalog_message.status == "completed"
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 0


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [("revoke", "AUTH_REQUIRED"), ("delete", "SOURCE_UNAVAILABLE")],
)
def test_catalog_change_between_emit_and_persist_cannot_complete_stale_answer(
    agent, database, monkeypatch, change, expected_code
):
    client, _, manager = agent
    base = create_public_knowledge_base(client, agent[1], "目录并发核验库")
    _, run_id = new_run(agent, "我当前有权限的知识库有哪些？")
    original = Execution.persist_terminal
    emitted = []

    async def change_before_persist(self, state):
        assert self.catalog_outputs
        assert "目录并发核验库" in self.buffer
        emitted.append(True)
        with Session(database.engine) as session, session.begin():
            if change == "revoke":
                session.get(User, database.user_id()).credential_version += 1
            else:
                row = session.get(KnowledgeBase, int(base["id"]))
                now = datetime.now(UTC)
                batch = DeletionBatch(
                    root_type="knowledge_base",
                    root_id=row.id,
                    root_name=row.name,
                    deleted_at=now,
                    purge_after=now + timedelta(days=30),
                )
                session.add(batch)
                session.flush()
                row.deleted_at = batch.deleted_at
                row.purge_after = batch.purge_after
                row.deletion_batch_id = batch.id
        return await original(self, state)

    monkeypatch.setattr(Execution, "persist_terminal", change_before_persist)
    assert client.portal.call(manager.execute, run_id)
    assert emitted == [True]

    with Session(database.engine) as session:
        run = session.get(AgentRun, run_id)
        message = session.get(Message, run.assistant_message_id)
        assert run.status == "failed" and run.failure_code == expected_code
        assert message.status == "failed"
        assert "目录并发核验库" not in (message.content_text or "")
        assert (
            session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(ToolCall.agent_run_id == run_id, ToolCall.status == "success")
            )
            == 1
        )


def test_run_deadline_reports_limit_and_does_not_complete_partial_answer(agent, database):
    client, headers, manager = agent

    class SlowModel(Model):
        async def answer(self, messages):
            yield "未完成的回答。"
            await asyncio.sleep(30)
            yield "不应保存的结尾。"

    manager.model = SlowModel()
    manager.config.run_timeout_seconds = 1
    _, run_id = new_run(agent)
    started = time.monotonic()
    assert client.portal.call(manager.execute, run_id)
    assert time.monotonic() - started < 10
    state = snapshot(agent, run_id)
    assert state["status"] == "failed"
    assert state["failure_code"] == "RUN_LIMIT_REACHED"
    assert "处理限制" in state["failure_message"]
    events = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers)
    assert "event: run.failed" in events.text
    assert "RUN_LIMIT_REACHED" in events.text
    assert "event: run.completed" not in events.text
    with Session(database.engine) as session:
        run = session.get(AgentRun, run_id)
        assert session.get(Message, run.assistant_message_id).status == "failed"


def snapshot(agent, run_id):
    client, headers, _ = agent
    return client.get(f"/api/v1/agent-runs/{run_id}", headers=headers).json()["data"]


def test_conversation_idempotency_sequence_and_general_answer(agent, database):
    client, headers, manager = agent
    conversation_id, run_id = new_run(agent, key="message-once")
    duplicate = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请写一段欢迎词。"},
        "message-once",
    )
    assert duplicate.json()["data"]["id"] == str(run_id)
    conflict = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "另一请求。"},
    )
    assert conflict.status_code == 409
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    result = client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    ).json()["data"]
    assert [row["role"] for row in result] == ["user", "assistant"]
    assert [row["sequence"] for row in result] == ["1", "2"]
    assert result[-1]["content_text"].endswith("完整回答。")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM langgraph_checkpoint.checkpoints")) == 0
    assert not client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 2


@pytest.mark.parametrize("attempt", range(4))
def test_unconfigured_model_is_explicit_and_replay_is_safe(agent, monkeypatch, attempt):
    client, _, manager = agent
    errors = []
    original = Execution.finish

    async def observed(self, code, **kwargs):
        try:
            return await original(self, code, **kwargs)
        except Exception as error:
            errors.append((code, type(error).__name__, getattr(error, "code", None)))
            raise

    monkeypatch.setattr(Execution, "finish", observed)
    manager.model = None
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert (
        state["status"] == "failed" and state["failure_code"] == "MODEL_PROVIDER_NOT_CONFIGURED"
    ), errors


def test_owner_isolation_even_for_other_administrator_and_credential_revocation(agent, database):
    client, headers, manager = agent
    conversation_id, run_id = new_run(agent)
    with Session(database.engine) as session, session.begin():
        first = session.get(User, database.user_id())
        first_id = first.id
        other = User(
            username="另一管理员",
            username_key="另一管理员",
            role="admin",
            password_hash=first.password_hash,
        )
        session.add(other)
    other_headers = auth(client, "另一管理员", database.admin_password)
    for path in (
        f"/conversations/{conversation_id}",
        f"/conversations/{conversation_id}/messages",
        f"/agent-runs/{run_id}",
        f"/agent-runs/{run_id}/events",
    ):
        assert client.get("/api/v1" + path, headers=other_headers).status_code == 404
    with Session(database.engine) as session, session.begin():
        session.get(User, first_id).credential_version += 1
    assert client.get(f"/api/v1/agent-runs/{run_id}", headers=headers).status_code == 401
    client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        assert session.get(AgentRun, run_id).status == "failed"


def test_unconfigured_error_survives_a_transient_finalization_failure(agent, monkeypatch):
    client, _, manager = agent
    manager.model = None
    original = Execution.finish
    codes = []

    async def fail_once(self, code, **kwargs):
        codes.append(code)
        if len(codes) == 1:
            raise RuntimeError("transient finalization error")
        return await original(self, code, **kwargs)

    monkeypatch.setattr(Execution, "finish", fail_once)
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    assert codes == ["MODEL_PROVIDER_NOT_CONFIGURED", "MODEL_PROVIDER_NOT_CONFIGURED"]
    assert snapshot(agent, run_id)["failure_code"] == "MODEL_PROVIDER_NOT_CONFIGURED"


def test_stop_preserves_checkpoint_and_resume_reuses_assistant(agent, database):
    client, headers, manager = agent
    manager.model.delay = True
    conversation_id, run_id = new_run(agent, content="UNIQUE_USER_BODY_849 请写一段欢迎词。")
    future = client.portal.start_task_soon(manager.execute, run_id)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and snapshot(agent, run_id)["current_step"] != "生成":
        time.sleep(0.05)
    assert snapshot(agent, run_id)["current_step"] == "生成"
    time.sleep(0.15)
    stopped = client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=headers)
    assert stopped.status_code == 200, stopped.text
    future.result(timeout=10)
    first_id = snapshot(agent, run_id)["assistant_message_id"]
    assert snapshot(agent, run_id)["status"] == "cancelled"
    assert snapshot(agent, run_id)["started_at"] is not None
    with database.engine.connect() as connection:
        blobs = (
            connection.execute(text("SELECT blob FROM langgraph_checkpoint.checkpoint_blobs"))
            .scalars()
            .all()
        )
        assert blobs
        assert all(b"UNIQUE_USER_BODY_849" not in bytes(blob) for blob in blobs if blob is not None)
        writes = (
            connection.execute(text("SELECT blob FROM langgraph_checkpoint.checkpoint_writes"))
            .scalars()
            .all()
        )
        meta = connection.execute(
            text("SELECT checkpoint, metadata FROM langgraph_checkpoint.checkpoints")
        ).all()
        assert all(b"UNIQUE_USER_BODY_849" not in bytes(blob) for blob in writes if blob)
        assert "UNIQUE_USER_BODY_849" not in str(meta)
    manager.model.delay = False
    resumed = client.post(f"/api/v1/agent-runs/{run_id}/resume", headers=headers)
    assert resumed.status_code == 202, resumed.text
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert snapshot(agent, run_id)["assistant_message_id"] == first_id
    with Session(database.engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == int(conversation_id))
            )
            == 2
        )


def test_duplicate_runner_cannot_enter_same_run(agent):
    client, _, manager = agent
    manager.model.delay = True
    _, run_id = new_run(agent)
    first = client.portal.start_task_soon(manager.execute, run_id)
    time.sleep(0.2)
    assert client.portal.call(manager.execute, run_id) is False
    client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=agent[1])
    first.result(timeout=10)


def test_terminal_sse_snapshot_and_secret_input_rejection(agent, database):
    client, headers, manager = agent
    conversation_id, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    result = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers)
    assert result.status_code == 200 and "text/event-stream" in result.headers["content-type"]
    assert "event: run.completed" in result.text
    assert "checkpoint" not in result.text and "plan_steps" not in result.text
    response = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "密码: secret-should-not-persist"},
    )
    assert response.status_code == 422 and "secret-should-not-persist" not in response.text
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 2


def test_conversation_rename_version_and_pagination(agent):
    client, headers, _ = agent
    conversation_id, _ = new_run(agent)
    response = client.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=headers,
        json={"title": "新名称", "row_version": 1},
    )
    assert response.status_code == 409
    current = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()["data"]
    response = client.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=headers,
        json={"title": "新名称", "row_version": current["row_version"]},
    )
    assert response.status_code == 200
    post(client, headers, "/api/v1/conversations", {"title": "另一个会话"})
    page = client.get("/api/v1/conversations?limit=1", headers=headers).json()
    following = client.get(
        "/api/v1/conversations",
        headers=headers,
        params={"cursor": page["meta"]["next_cursor"], "limit": 1},
    ).json()
    assert page["data"][0]["id"] != following["data"][0]["id"]


@pytest.mark.parametrize("boundary", ["generate", "persist"])
def test_process_interruption_recovers_discarded_draft(agent, database, monkeypatch, boundary):
    client, _, manager = agent
    _, run_id = new_run(agent)
    original = Execution.persist_terminal

    async def interrupt():
        reached = asyncio.Event()
        if boundary == "persist":

            async def pause(self, state):
                reached.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(Execution, "persist_terminal", pause)
        else:

            async def pause_answer(messages):
                yield "暂存片段。\n"
                reached.set()
                await asyncio.Event().wait()

            manager.model.answer = pause_answer
        task = asyncio.create_task(manager.execute(run_id))
        await asyncio.wait_for(reached.wait(), 8)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    client.portal.call(interrupt)
    assert snapshot(agent, run_id)["status"] == "running"
    monkeypatch.setattr(Execution, "persist_terminal", original)
    manager.model = Model()
    assert client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "completed", state
    with Session(database.engine) as session:
        assert session.get(Message, int(state["assistant_message_id"])).content_text.endswith(
            "完整回答。"
        )
        assert session.scalar(select(func.count()).select_from(Message)) == 2


def test_missing_checkpoint_fails_recovery_and_pending_stop_is_not_resumable(agent, database):
    client, headers, manager = agent
    _, run_id = new_run(agent)
    with Session(database.engine) as session, session.begin():
        session.get(AgentRun, run_id).status = "running"
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "failed"
    _, stopped_id = new_run(agent)
    assert client.post(f"/api/v1/agent-runs/{stopped_id}/stop", headers=headers).status_code == 200
    assert snapshot(agent, stopped_id)["started_at"] is None
    assert not client.portal.call(manager.execute, stopped_id)
    assert (
        client.post(f"/api/v1/agent-runs/{stopped_id}/resume", headers=headers).status_code == 409
    )


@pytest.mark.parametrize("revoke", [False, True])
def test_live_sse_rechecks_identity_before_releasing_text(agent, database, revoke):
    client, headers, manager = agent
    _, run_id = new_run(agent)
    subscribe = manager.bus.subscribe
    first_chunk_read = asyncio.Event()

    async def answer(messages):
        # No newline and fewer than 128 characters: the client must receive this
        # while the provider is still waiting to generate the rest of the answer.
        yield "仅允许当前用户接收的片段。"
        await asyncio.wait_for(first_chunk_read.wait(), timeout=5)
        yield "最终结果。"

    @asynccontextmanager
    async def start_and_subscribe(number):
        async with subscribe(number) as read:

            async def checked_read(seconds):
                item = await read(seconds)
                if revoke and item and item["event"] == "answer.delta":
                    with Session(database.engine) as session, session.begin():
                        session.get(User, database.user_id()).credential_version += 1
                if item and item["event"] == "answer.delta":
                    first_chunk_read.set()
                return item

            task = asyncio.create_task(manager.execute(number))
            try:
                yield checked_read
            finally:
                await task

    manager.model.answer = answer
    manager.bus.subscribe = start_and_subscribe
    result = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers)
    assert result.status_code == 200
    if revoke:
        assert "event: run.failed" in result.text
        assert "仅允许当前用户接收的片段" not in result.text
        with Session(database.engine) as session:
            assert session.get(AgentRun, run_id).status == "failed"
    else:
        assert "event: answer.delta" in result.text and "event: run.completed" in result.text
        assert "仅允许当前用户接收的片段" in result.text
        assert first_chunk_read.is_set()


def test_invalid_plan_is_bounded_and_recovers_with_clarification(agent):
    client, _, manager = agent

    async def invalid(messages, tools, feedback):
        raise ValueError("raw-model-output-must-not-escape")

    manager.model.plan = invalid
    conversation, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "completed" and state["replan_count"] == 2
    result = client.get(f"/api/v1/conversations/{conversation}/messages", headers=agent[1]).json()
    assert "请补充" in result["data"][-1]["content_text"]
    assert "raw-model-output" not in str(result)


@pytest.mark.parametrize(
    ("question", "clarification", "expected_plan_calls"),
    [
        ("请用一句话回答，不生成文件。", None, 1),
        ("起草一份报告，不生成 DOCX/PDF/XLSX 文件。", None, 1),
        ("把内容保存成文件。", "请明确文件格式：DOCX、PDF、Markdown、XLSX 或数据库 CSV。", 0),
        (
            "起草一份报告并保存成文件。",
            "请明确文件格式：DOCX、PDF、Markdown、XLSX 或数据库 CSV。",
            0,
        ),
    ],
)
def test_file_request_guard_keeps_negation_and_specific_clarification(
    agent, database, question, clarification, expected_plan_calls
):
    from enterprise_mind.agent.generation_models import ContentDraft, GenerationTask

    client, headers, manager = agent
    conversation, run_id = new_run(agent, question)
    client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "completed"
    assert manager.model.plan_calls == expected_plan_calls
    result = client.get(f"/api/v1/conversations/{conversation}/messages", headers=headers).json()
    answer = result["data"][-1]
    if clarification:
        assert answer["content_text"] == clarification
        assert not answer.get("content_parts")
    else:
        assert "完整回答" in answer["content_text"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(GenerationTask)) == 0
        if clarification:
            assert session.scalar(select(func.count()).select_from(ContentDraft)) == 0


@pytest.mark.parametrize(
    ("intent", "clarification", "expected_drafts"),
    [
        ("general", "已了解，请继续。", 1),
        ("clarify", "请说明报告的主题。", 0),
        ("clarify", None, 0),
    ],
)
def test_only_clarification_intent_blocks_draft_and_never_becomes_one(
    agent, database, intent, clarification, expected_drafts
):
    from enterprise_mind.agent.generation_models import ContentDraft

    client, headers, manager = agent

    async def plan(messages, tools, feedback):
        return Plan(
            intent_kind=intent,
            requires_enterprise_facts=False,
            clarification=clarification,
        )

    manager.model.plan = plan
    conversation, run_id = new_run(agent, "起草一份报告。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    result = client.get(f"/api/v1/conversations/{conversation}/messages", headers=headers).json()
    answer = result["data"][-1]
    if intent == "clarify":
        assert answer["content_text"] == (
            clarification or "请补充要处理的资源和具体目标，以便确定操作范围。"
        )
        assert not answer.get("content_parts")
    else:
        assert "完整回答" in answer["content_text"]
        assert answer["content_parts"][0]["type"] == "content_draft"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ContentDraft)) == expected_drafts


@pytest.mark.parametrize("generation_request", ["起草一份报告", "生成 PDF 报告"])
def test_no_evidence_refusal_never_creates_a_draft_or_file(agent, database, generation_request):
    from test_rag_pipeline import TestEmbedding

    from enterprise_mind.agent.generation_models import ContentDraft, GenerationTask
    from enterprise_mind.agent.schemas import ProposedTool
    from enterprise_mind.rag.config import RAGConfig
    from enterprise_mind.rag.repository import RetrievalRepository
    from enterprise_mind.rag.retrieval import RetrievalService

    client, headers, manager = agent
    config = RAGConfig()
    embedding = TestEmbedding()
    manager.retrieval = RetrievalService(
        RetrievalRepository(manager.sessions, config, embedding.model_key, embedding.dimensions),
        embedding,
        None,
        config,
    )

    async def plan(messages, tools, feedback):
        return Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            tools=[
                ProposedTool(
                    key="lookup",
                    tool_name="knowledge_retrieve",
                    arguments={"question": "不存在的合成资料"},
                )
            ],
        )

    manager.model.plan = plan
    conversation, run_id = new_run(agent, f"根据知识库资料{generation_request}。")
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    answer = client.get(f"/api/v1/conversations/{conversation}/messages", headers=headers).json()[
        "data"
    ][-1]
    assert "没有找到足够依据" in answer["content_text"]
    assert not answer.get("content_parts")
    assert not answer.get("sources")
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ContentDraft)) == 0
        assert session.scalar(select(func.count()).select_from(GenerationTask)) == 0


def test_manual_draft_exports_reappear_in_history_without_duplicates(agent, database):
    from enterprise_mind.agent.generation_models import GenerationTask

    client, headers, manager = agent
    conversation_id, run_id = new_run(agent, "起草一份报告，只输出草稿，不生成文件。")
    client.portal.call(manager.execute, run_id)
    path = f"/api/v1/conversations/{conversation_id}/messages"
    answer = client.get(path, headers=headers).json()["data"][-1]
    draft_part = next(p for p in answer["content_parts"] if p["type"] == "content_draft")
    render_path = f"/api/v1/drafts/{draft_part['draft_id']}/render"
    body = {"expected_revision": draft_part["revision"], "format": "docx"}
    first = post(client, headers, render_path, body, "history-docx")
    assert first.status_code == 202
    task_id = first.json()["data"]["task_id"]
    assert (
        post(client, headers, render_path, body, "history-docx").json()["data"]["task_id"]
        == task_id
    )
    second = post(client, headers, render_path, {**body, "format": "md"}, "history-md")
    assert second.status_code == 202
    second_id = second.json()["data"]["task_id"]
    # Existing automatic task references must not duplicate reconstructed ones;
    # old durable tasks with no message part must also reappear.
    with Session(database.engine) as session, session.begin():
        message = session.get(Message, int(answer["id"]))
        message.content_parts = [
            *message.content_parts,
            {
                "type": "generated_file_task",
                "task_id": task_id,
                "kind": "content_render",
                "format": "docx",
                "status": "queued",
                "scope": None,
            },
        ]
        task = session.get(GenerationTask, int(second_id))
        task.status = "cancelled"
    for _ in range(2):
        restored = client.get(path, headers=headers).json()["data"][-1]
        tasks = [p for p in restored["content_parts"] if p["type"] == "generated_file_task"]
        assert [p["task_id"] for p in tasks] == [task_id, second_id]
        assert tasks[-1]["status"] == "cancelled"
    other = post(
        client,
        headers,
        "/api/v1/admin/users",
        {
            "username": "导出历史其他用户",
            "password": "History-Other-User-2026!",
            "role": "admin",
        },
    )
    assert other.status_code == 201
    other_headers = auth(client, "导出历史其他用户", "History-Other-User-2026!")
    assert client.get(path, headers=other_headers).status_code == 404


def test_long_history_is_summarized_and_reused_as_nonfactual_context(agent, database):
    client, headers, manager = agent
    manager.config.summary_threshold_tokens = 100
    manager.config.recent_message_count = 2
    result = post(client, headers, "/api/v1/conversations", {"title": "长会话"})
    conversation_id = int(result.json()["data"]["id"])
    with Session(database.engine) as session, session.begin():
        item = session.get(Conversation, conversation_id)
        for number in range(1, 9):
            session.add(
                Message(
                    conversation_id=conversation_id,
                    sequence=number,
                    role="user" if number % 2 else "assistant",
                    status="completed",
                    content_text="过去的讨论主题。" * 15,
                )
            )
        item.next_message_sequence = 9
    result = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "继续撰写介绍"},
    )
    run_id = int(result.json()["data"]["id"])
    observed = []
    original = manager.model.plan

    async def plan(messages, tools, feedback):
        observed.extend(item.content for item in messages)
        return await original(messages, tools, feedback)

    manager.model.plan = plan
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert any("不是企业事实依据" in item for item in observed)
    with Session(database.engine) as session:
        summary = session.scalars(select(ConversationSummary)).one()
        assert summary.through_sequence == 6 and summary.source_token_count >= 100


def test_failed_checkpoint_cleanup_is_retried_without_changing_answer(agent, database, monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    client, _, manager = agent
    _, run_id = new_run(agent)
    original = AsyncPostgresSaver.adelete_thread

    async def unavailable(self, thread_id):
        raise RuntimeError("temporary cleanup failure")

    monkeypatch.setattr(AsyncPostgresSaver, "adelete_thread", unavailable)
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM langgraph_checkpoint.checkpoints")) > 0
    monkeypatch.setattr(AsyncPostgresSaver, "adelete_thread", original)
    client.portal.call(manager.cleanup_terminal)
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM langgraph_checkpoint.checkpoints")) == 0
