# ruff: noqa: F811
"""Controlled failures must settle visibly without losing the next user turn."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent import Model, agent, new_run, post, seed_long_history, snapshot  # noqa: F401

from enterprise_mind.agent.models import AgentRun, ConversationSummary, Message
from enterprise_mind.rag.providers import ProviderFailure

pytestmark = pytest.mark.integration


def test_general_introduction_receives_configured_runtime_identity(agent):
    client, headers, manager = agent
    observed = []

    class IdentityAwareModel(Model):
        model_name = "test-provider/configured-model"

        async def answer(self, messages):
            observed.extend(messages)
            yield "我是 EnterpriseMind 知识助手。"

    manager.model = IdentityAwareModel()
    conversation = post(client, headers, "/api/v1/conversations", {"title": "一般自我介绍"})
    submitted = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation.json()['data']['id']}/messages",
        {"content_text": "你好，简单介绍一下你自己。"},
    )
    run_id = int(submitted.json()["data"]["id"])
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert observed and observed[0].type == "system"
    assert "test-provider/configured-model" in observed[0].content
    assert "EnterpriseMind" in observed[0].content


@pytest.mark.parametrize("chunks", [[], ["", " \n", "\t"]])
def test_blank_stream_fails_instead_of_completing_an_invisible_answer(agent, database, chunks):
    client, headers, manager = agent

    class BlankModel(Model):
        async def answer(self, messages):
            for chunk in chunks:
                yield chunk

    manager.model = BlankModel()
    conversation_id, run_id = new_run(agent)
    assert client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "failed" and state["failure_code"] == "MODEL_PROVIDER_FAILED"
    events = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers).text
    assert "event: run.failed" in events and "event: run.completed" not in events
    with Session(database.engine) as session:
        message = session.get(Message, int(state["assistant_message_id"]))
        assert message.status == "failed" and message.content_text.strip()
    manager.model = Model()
    next_run = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请再写一句欢迎词。"},
    )
    assert next_run.status_code == 202
    next_id = int(next_run.json()["data"]["id"])
    assert client.portal.call(manager.execute, next_id)
    assert snapshot(agent, next_id)["status"] == "completed"


def test_response_timeout_keeps_partial_answer_failed_and_next_turn_available(agent, database):
    client, headers, manager = agent

    class TimeoutModel(Model):
        async def answer(self, messages):
            yield "尚未完成的临时片段"
            raise ProviderFailure("MODEL_RESPONSE_TIMEOUT")

    manager.model = TimeoutModel()
    conversation_id, run_id = new_run(agent)
    assert client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "failed" and state["failure_code"] == "MODEL_RESPONSE_TIMEOUT"
    events = client.get(f"/api/v1/agent-runs/{run_id}/events", headers=headers).text
    assert "MODEL_RESPONSE_TIMEOUT" in events and "event: run.failed" in events
    assert "event: run.completed" not in events
    with Session(database.engine) as session:
        run = session.get(AgentRun, run_id)
        message = session.get(Message, run.assistant_message_id)
        assert message.status == "failed" and "尚未完成" not in message.content_text
    manager.model = Model()
    submitted = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请写一句简短的欢迎词。"},
    )
    assert submitted.status_code == 202
    next_id = int(submitted.json()["data"]["id"])
    assert client.portal.call(manager.execute, next_id)
    assert snapshot(agent, next_id)["status"] == "completed"


def test_optional_summary_failure_uses_recent_history_and_still_answers(agent, database):
    client, headers, manager = agent
    created = post(client, headers, "/api/v1/conversations", {"title": "摘要失败降级"})
    conversation_id = int(created.json()["data"]["id"])
    seed_long_history(database, conversation_id)
    manager.config.summary_threshold_tokens = 1
    manager.config.recent_message_count = 2
    summaries, observed = [], []

    class SummaryFailureModel(Model):
        async def summarize(self, messages):
            summaries.append(True)
            raise ProviderFailure("MODEL_PROVIDER_FAILED")

        async def plan(self, messages, tools, feedback):
            observed.extend(str(message.content) for message in messages)
            return await super().plan(messages, tools, feedback)

    manager.model = SummaryFailureModel()
    submitted = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/messages",
        {"content_text": "请继续整理一般性的阅读建议。"},
    )
    run_id = int(submitted.json()["data"]["id"])
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert summaries == [True] and any("旧历史中的讨论内容" in item for item in observed)
    assert observed[-1] == "请继续整理一般性的阅读建议。"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ConversationSummary)) == 0
