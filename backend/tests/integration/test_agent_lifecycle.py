# ruff: noqa: F811

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_agent import Model, agent, new_run, post, snapshot  # noqa: F401
from test_management import auth

from enterprise_mind.agent.models import Conversation, Message, ToolCall
from enterprise_mind.agent.schemas import Plan
from enterprise_mind.identity.models import User
from enterprise_mind.ingestion.models import Folder, OutboxEvent
from enterprise_mind.knowledge.pending_models import PendingAction

pytestmark = pytest.mark.integration


class WriteModel(Model):
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments
        self.results = []

    async def plan(self, messages, tools, feedback):
        assert self.name in {tool.name for tool in tools}
        return Plan(
            intent_kind="general",
            requires_enterprise_facts=False,
            tools=[{"key": "write", "tool_name": self.name, "arguments": self.arguments}],
        )

    async def answer(self, messages):
        self.results = [str(m.content) for m in messages]
        yield "操作结果已返回。"


def setup_folder(active_agent, base_name="base", folder_name="folder", child=False):
    client, headers, _ = active_agent
    base = post(client, headers, "/api/v1/knowledge-bases", {"name": base_name, "type": "public"})
    assert base.status_code == 201, base.text
    identifier = base.json()["data"]["id"]
    folder = post(
        client, headers, f"/api/v1/knowledge-bases/{identifier}/folders", {"name": folder_name}
    )
    assert folder.status_code == 201, folder.text
    folder_id = folder.json()["data"]["id"]
    if child:
        result = post(
            client,
            headers,
            f"/api/v1/knowledge-bases/{identifier}/folders",
            {"name": "child", "parent_id": folder_id},
        )
        assert result.status_code == 201, result.text
    return identifier, folder_id


def pending(database, run_id):
    with Session(database.engine) as session:
        action = session.scalar(
            select(PendingAction).where(
                PendingAction.agent_run_id == run_id, PendingAction.status == "pending"
            )
        )
        assert action is not None
        return action.id, action.action_type


def respond(active_agent, action_id, decision="accept", selected=None):
    client, headers, _ = active_agent
    return post(
        client,
        headers,
        f"/api/v1/pending-actions/{action_id}/responses",
        {"decision": decision, "selected_target_id": selected},
    )


def test_agent_low_risk_mutation_once_and_safe_result(agent, database):  # noqa: F811
    client, _, manager = agent
    base, folder = setup_folder(agent)
    manager.model = WriteModel(
        "folder_update", {"target": {"id": folder}, "changes": {"name": "renamed"}}
    )
    _, run_id = new_run(agent, "将文件夹改名")
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert not client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        row = session.get(Folder, int(folder))
        assert row.name == "renamed" and row.row_version == 2
        call = session.scalars(select(ToolCall)).one()
        assert call.arguments["target"] == {"id": folder}
        assert call.status == "success" and call.result_summary["name"] == "renamed"
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 0
    assert any("renamed" in result for result in manager.model.results)


def test_agent_high_risk_interrupt_confirm_resume_and_checkpoint_cleanup(agent, database):  # noqa: F811
    client, headers, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent, "删除文件夹及其内容")
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "waiting_confirmation"
    identifier, kind = pending(database, run_id)
    assert kind == "confirmation"
    assert not client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).deleted_at is None
    action = client.get(f"/api/v1/pending-actions/{identifier}", headers=headers)
    assert action.status_code == 200 and "30" in action.json()["data"]["prompt_text"]
    assert respond(agent, identifier).status_code == 200
    # The HTTP acknowledgement only accepts; the same gateway applies the business operation.
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).deleted_at is None
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).deleted_at is not None
        assert session.scalars(select(ToolCall)).one().status == "success"
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM langgraph_checkpoint.checkpoints")) == 0


def test_agent_disambiguation_then_separate_confirmation_and_rejection(agent, database):  # noqa: F811
    client, headers, manager = agent
    _, first = setup_folder(agent, "one", "same", child=True)
    _, second = setup_folder(agent, "two", "same", child=True)
    manager.model = WriteModel("folder_delete", {"target": {"name": "same"}})
    _, run_id = new_run(agent, "删除 same 文件夹")
    assert client.portal.call(manager.execute, run_id)
    identifier, kind = pending(database, run_id)
    assert kind == "disambiguation"
    targets = client.get(f"/api/v1/pending-actions/{identifier}", headers=headers).json()["data"][
        "targets"
    ]
    assert {target["id"] for target in targets} == {first, second}
    assert respond(agent, identifier, selected="999999").status_code == 422
    assert respond(agent, identifier, selected=second).status_code == 200
    assert client.portal.call(manager.execute, run_id)
    confirm_id, kind = pending(database, run_id)
    assert kind == "confirmation" and identifier != confirm_id
    assert respond(agent, confirm_id, "reject").status_code == 200
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        assert session.get(Folder, int(first)).deleted_at is None
        assert session.get(Folder, int(second)).deleted_at is None
        answer = session.scalar(select(Message.content_text).where(Message.role == "assistant"))
        assert "未执行" in answer


def test_agent_stop_waiting_cancels_confirmation_and_resume_requires_new_one(agent, database):  # noqa: F811
    client, headers, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    original, _ = pending(database, run_id)
    assert client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=headers).status_code == 200
    assert respond(agent, original).status_code == 409
    assert client.post(f"/api/v1/agent-runs/{run_id}/resume", headers=headers).status_code == 202
    client.portal.call(manager.execute, run_id)
    new_id, _ = pending(database, run_id)
    assert new_id != original


def test_resume_waits_for_stopping_graph_to_release_run_lock(agent, database):  # noqa: F811
    client, headers, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    assert client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=headers).status_code == 200

    acquired = threading.Event()

    def finishing_graph() -> None:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": -int(run_id)})
            connection.commit()
            acquired.set()
            time.sleep(0.4)
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": -int(run_id)})
            connection.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(finishing_graph)
        assert acquired.wait(timeout=5)
        started = time.monotonic()
        response = client.post(f"/api/v1/agent-runs/{run_id}/resume", headers=headers)
        elapsed = time.monotonic() - started
        future.result(timeout=5)
    assert response.status_code == 202, response.text
    assert elapsed >= 0.2


def test_non_owner_resume_is_rejected_before_waiting_for_run_lock(agent, database):  # noqa: F811
    client, headers, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    assert client.post(f"/api/v1/agent-runs/{run_id}/stop", headers=headers).status_code == 200
    with Session(database.engine) as session, session.begin():
        owner = session.get(User, database.user_id())
        session.add(
            User(
                username="继续任务隔离管理员",
                username_key="继续任务隔离管理员",
                role="admin",
                password_hash=owner.password_hash,
            )
        )
    foreign_headers = auth(client, "继续任务隔离管理员", database.admin_password)
    acquired = threading.Event()

    def finishing_graph() -> None:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": -int(run_id)})
            connection.commit()
            acquired.set()
            time.sleep(1.5)
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": -int(run_id)})
            connection.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(finishing_graph)
        assert acquired.wait(timeout=5)
        started = time.monotonic()
        response = client.post(f"/api/v1/agent-runs/{run_id}/resume", headers=foreign_headers)
        elapsed = time.monotonic() - started
        future.result(timeout=5)
    assert response.status_code == 404
    assert elapsed < 1.0


def test_confirmation_changed_after_acceptance_fails_before_mutation(agent, database):  # noqa: F811
    client, headers, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    identifier, _ = pending(database, run_id)
    assert respond(agent, identifier).status_code == 200
    result = client.patch(
        f"/api/v1/folders/{folder}", headers=headers, json={"row_version": 1, "name": "changed"}
    )
    assert result.status_code == 200
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "failed"
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).deleted_at is None


def test_conversation_confirmation_deletes_aggregate_and_durable_checkpoint_cleanup(
    agent, database
):  # noqa: F811
    client, headers, manager = agent
    conversation_id, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    current = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()["data"]
    action = post(
        client,
        headers,
        f"/api/v1/conversations/{conversation_id}/deletion-requests",
        {"row_version": current["row_version"]},
    )
    assert action.status_code == 200, action.text
    assert respond(agent, action.json()["data"]["id"]).status_code == 200
    with Session(database.engine) as session:
        assert session.get(Conversation, int(conversation_id)) is None
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        assert (
            session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_type == "conversation.checkpoints_cleanup.requested"
                )
            )
            is not None
        )
    client.portal.call(manager.cleanup_terminal)
    with Session(database.engine) as session:
        assert (
            session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_type == "conversation.checkpoints_cleanup.requested"
                )
            )
            is None
        )


def test_agent_expiry_recovers_without_ingestion_dispatcher(agent, database):
    from datetime import UTC, datetime, timedelta

    client, _, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    identifier, _ = pending(database, run_id)
    with Session(database.engine) as session, session.begin():
        action = session.get(PendingAction, identifier)
        action.created_at = datetime.now(UTC) - timedelta(hours=2)
        action.expires_at = datetime.now(UTC) - timedelta(hours=1)
    client.portal.call(manager.execute, run_id)
    state = snapshot(agent, run_id)
    assert state["status"] == "failed" and state["failure_code"] == "PENDING_ACTION_EXPIRED"
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).deleted_at is None


def test_mutation_committed_before_checkpoint_is_not_applied_twice(agent, database, monkeypatch):
    import asyncio

    from enterprise_mind.agent.runner import Execution
    from enterprise_mind.knowledge.models import DeletionBatch

    client, _, manager = agent
    _, folder = setup_folder(agent, child=True)
    manager.model = WriteModel("folder_delete", {"target": {"id": folder}})
    _, run_id = new_run(agent)
    client.portal.call(manager.execute, run_id)
    identifier, _ = pending(database, run_id)
    assert respond(agent, identifier).status_code == 200
    original = Execution.execute_mutation

    async def crash():
        reached = asyncio.Event()

        async def pause(self, state):
            result = await original(self, state)
            reached.set()
            await asyncio.Event().wait()
            return result

        monkeypatch.setattr(Execution, "execute_mutation", pause)
        task = asyncio.create_task(manager.execute(run_id))
        await asyncio.wait_for(reached.wait(), 8)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    client.portal.call(crash)
    monkeypatch.setattr(Execution, "execute_mutation", original)
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(DeletionBatch)) == 1
        assert session.get(Folder, int(folder)).row_version == 2


def test_missing_change_requests_clarification_without_mutating_resource(agent, database):
    client, _, manager = agent
    _, folder = setup_folder(agent)
    manager.model = WriteModel("folder_update", {"target": {"id": folder}, "changes": {}})
    _, run_id = new_run(agent, "修改文件夹")
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        assert session.get(Folder, int(folder)).row_version == 1
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 0
        answer = session.scalar(select(Message.content_text).where(Message.role == "assistant"))
        assert "请补充" in answer
