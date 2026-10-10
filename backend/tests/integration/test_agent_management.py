# ruff: noqa: F811

import asyncio
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent import Model, agent, new_run, post, snapshot  # noqa: F401
from test_agent_lifecycle import WriteModel
from test_management import PASSWORD, D, U, created, setup_organization

from enterprise_mind.agent.checkpoints import open_saver
from enterprise_mind.agent.models import Message, ToolCall
from enterprise_mind.agent.runner import Execution
from enterprise_mind.agent.schemas import Plan
from enterprise_mind.identity.models import Department, User
from enterprise_mind.knowledge.pending_models import PendingAction
from enterprise_mind.rag.providers import ProviderFailure

pytestmark = pytest.mark.integration


def execute(active_agent, name, arguments):
    client, _, manager = active_agent
    manager.model = WriteModel(name, arguments)
    conversation_id, run_id = new_run(active_agent, "执行管理操作")
    assert client.portal.call(manager.execute, run_id)
    return conversation_id, run_id


def answer(active_agent, conversation_id):
    client, headers, _ = active_agent
    return client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers).json()[
        "data"
    ][-1]


class ActionModel(Model):
    def __init__(self, action):
        self.action = action

    async def plan(self, messages, tools, feedback):
        return Plan(
            intent_kind="general", requires_enterprise_facts=False, client_action=self.action
        )

    async def answer(self, messages):
        raise AssertionError("navigation must use the controlled explanation")
        yield "unreachable"


def action_run(active_agent, action):
    client, _, manager = active_agent
    manager.model = ActionModel(action)
    conversation_id, run_id = new_run(active_agent, "请打开对应的用户管理表单")
    assert client.portal.call(manager.execute, run_id)
    return conversation_id, run_id


def test_admin_department_tools_create_update_delete_are_low_risk_and_idempotent(agent, database):
    client, _, manager = agent
    _, run_id = execute(agent, "department_create", {"name": "研发", "status": "active"})
    assert snapshot(agent, run_id)["status"] == "completed"
    assert not client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        row = session.scalars(select(Department)).one()
        identifier = str(row.id)
    _, run_id = execute(
        agent, "department_update", {"target": {"name": "研发"}, "changes": {"name": "产品研发"}}
    )
    assert snapshot(agent, run_id)["status"] == "completed"
    _, run_id = execute(agent, "department_delete", {"target": {"id": identifier}})
    assert snapshot(agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        assert session.get(Department, int(identifier)) is None
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 0
        calls = list(session.scalars(select(ToolCall).order_by(ToolCall.id)))
        assert len(calls) == 3 and all(c.risk_level == "low" for c in calls)
        assert calls[-1].result_summary == {"id": identifier, "status": "deleted"}


def test_user_tool_reuses_permission_changes_without_exposing_credentials(agent, database):
    client, headers, _ = agent
    _, second, user, ordinary = setup_organization(client, headers)
    _, run_id = execute(
        agent,
        "user_update",
        {"target": {"name": user["username"]}, "changes": {"department_id": second["id"]}},
    )
    assert snapshot(agent, run_id)["status"] == "completed"
    current = client.get("/api/v1/auth/me", headers=ordinary).json()["data"]
    assert current["department"]["id"] == second["id"]
    with Session(database.engine) as session:
        call = session.scalars(select(ToolCall)).one()
        assert call.arguments["row_version"] == 1 and call.status == "success"
        assert call.result_summary["username"] == user["username"]
        payload = json.dumps(call.result_summary)
        assert "password" not in payload and "credential" not in payload


def test_committed_management_receipt_survives_incorrect_fact_classification(agent, database):
    client, _, manager = agent

    class Misclassified(WriteModel):
        async def plan(self, messages, tools, feedback):
            result = await super().plan(messages, tools, feedback)
            result.requires_enterprise_facts = True
            return result

        async def answer(self, messages):
            raise AssertionError("missing facts must not be invented")
            yield "unreachable"

    manager.model = Misclassified("department_create", {"name": "回执验证"})
    conversation_id, run_id = new_run(agent, "创建回执验证部门并查询报销标准")
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    content = answer(agent, conversation_id)["content_text"]
    assert "已创建部门“回执验证”" in content
    assert "没有找到足够依据" in content
    with Session(database.engine) as session:
        assert session.scalars(select(Department)).one().name == "回执验证"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("MODEL_PROVIDER_FAILED", "MODEL_PROVIDER_FAILED"),
        ("MODEL_PROVIDER_RATE_LIMITED", "MODEL_PROVIDER_RATE_LIMITED"),
        ("invalid_citation", "ANSWER_SOURCE_INVALID"),
    ],
)
def test_committed_write_receipt_survives_answer_failure(agent, database, failure, code):
    client, _, manager = agent

    class FailedAnswer(WriteModel):
        async def answer(self, messages):
            assert "[[source_1]]" not in messages[0].content
            if failure == "invalid_citation":
                yield "未经验证的模型回答 [[source_1]]"
                return
            yield "未经验证的模型回答，不应保留。\n"
            raise ProviderFailure(failure)

    manager.model = FailedAnswer("department_create", {"name": "故障回执验证"})
    conversation_id, run_id = new_run(agent, "创建故障回执验证部门")
    assert client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "failed"
    assert snapshot(agent, run_id)["failure_code"] == code
    content = answer(agent, conversation_id)["content_text"]
    assert "已创建部门“故障回执验证”" in content
    assert "上述操作已经执行。后续回答未能完成" in content
    assert "未经验证" not in content
    assert not client.portal.call(manager.execute, run_id)
    with Session(database.engine) as session:
        assert session.scalars(select(Department)).one().name == "故障回执验证"
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 1


@pytest.mark.parametrize("reference", ["user", "knowledge_base"])
def test_department_tool_cannot_delete_referenced_department(agent, database, reference):
    client, headers, _ = agent
    department = created(client, headers, D, {"name": "保留部门"})
    if reference == "user":
        created(
            client,
            headers,
            U,
            {
                "username": "成员",
                "password": PASSWORD,
                "role": "user",
                "department_id": department["id"],
            },
        )
    else:
        created(
            client,
            headers,
            "/api/v1/knowledge-bases",
            {
                "name": "资料",
                "type": "department",
                "department_id": department["id"],
            },
        )
    _, run_id = execute(agent, "department_delete", {"target": {"id": department["id"]}})
    assert snapshot(agent, run_id)["failure_code"] == "DEPARTMENT_IN_USE"
    with Session(database.engine) as session:
        assert session.get(Department, int(department["id"])) is not None


def test_ordinary_user_cannot_forge_admin_tool_or_navigation(agent, database):
    client, headers, manager = agent
    department, _, _, ordinary = setup_organization(client, headers)
    ordinary_agent = client, ordinary, manager

    class ForgedModel(Model):
        async def plan(self, messages, tools, feedback):
            names = {tool.name for tool in tools}
            assert "user_update" not in names and "department_delete" not in names
            return Plan(
                intent_kind="general",
                requires_enterprise_facts=False,
                tools=[
                    {
                        "key": "forge",
                        "tool_name": "department_delete",
                        "arguments": {"target": {"id": department["id"]}},
                    }
                ],
            )

    manager.model = ForgedModel()
    _, run_id = new_run(ordinary_agent)
    client.portal.call(manager.execute, run_id)
    assert snapshot(ordinary_agent, run_id)["failure_code"] == "PERMISSION_DENIED"
    conversation, run_id = action_run(ordinary_agent, {"action": "open_admin_user_create"})
    assert snapshot(ordinary_agent, run_id)["failure_code"] == "PERMISSION_DENIED"
    assert answer(ordinary_agent, conversation)["content_parts"] is None


def test_admin_tool_rechecks_target_version_before_execution(agent, database, monkeypatch):
    client, headers, _ = agent
    department = created(client, headers, D, {"name": "原始部门"})
    original = Execution.execute_mutation

    async def concurrent_change(self, state):
        with Session(database.engine) as session, session.begin():
            row = session.get(Department, int(department["id"]))
            row.name, row.name_key, row.row_version = "另一次修改", "另一次修改", 2
        return await original(self, state)

    monkeypatch.setattr(Execution, "execute_mutation", concurrent_change)
    _, run_id = execute(
        agent,
        "department_update",
        {
            "target": {"id": department["id"]},
            "changes": {"name": "过期修改"},
        },
    )
    assert snapshot(agent, run_id)["failure_code"] == "ROW_VERSION_CONFLICT"
    with Session(database.engine) as session:
        assert session.get(Department, int(department["id"])).name == "另一次修改"


def test_admin_tool_rechecks_actor_after_planning(agent, database, monkeypatch):
    client, headers, _ = agent
    department = created(client, headers, D, {"name": "部门"})
    original = Execution.execute_mutation

    async def revoked(self, state):
        with Session(database.engine) as session, session.begin():
            user = session.get(User, database.user_id())
            user.role, user.department_id, user.row_version = "user", int(department["id"]), 2
        return await original(self, state)

    monkeypatch.setattr(Execution, "execute_mutation", revoked)
    _, run_id = execute(agent, "department_create", {"name": "不应创建"})
    assert snapshot(agent, run_id)["status"] == "failed"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Department)) == 1


def test_admin_delete_commit_before_checkpoint_replays_without_target_lookup(
    agent, database, monkeypatch
):
    client, headers, manager = agent
    department = created(client, headers, D, {"name": "可删除"})
    manager.model = WriteModel("department_delete", {"target": {"id": department["id"]}})
    _, run_id = new_run(agent)
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
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    with Session(database.engine) as session:
        assert session.get(Department, int(department["id"])) is None
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 1


def test_missing_admin_target_clarifies_without_mutation(agent, database):
    conversation, run_id = execute(
        agent,
        "user_update",
        {
            "target": {"name": "不存在的用户"},
            "changes": {"status": "disabled"},
        },
    )
    assert snapshot(agent, run_id)["status"] == "completed"
    assert "请补充" in answer(agent, conversation)["content_text"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 0


def test_create_user_navigation_is_persisted_without_user_tool_or_pending_action(agent, database):
    conversation, run_id = action_run(
        agent,
        {
            "action": "open_admin_user_create",
            "prefill": {"username": "待创建用户", "role": "admin"},
        },
    )
    assert snapshot(agent, run_id)["status"] == "completed"
    output = answer(agent, conversation)
    assert output["message_type"] == "mixed" and "尚未创建" in output["content_text"]
    assert output["content_parts"] == [
        {
            "type": "client_action",
            "action": "open_admin_user_create",
            "prefill": {"username": "待创建用户", "role": "admin"},
            "available": True,
        }
    ]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 0


def test_reset_navigation_binds_server_target_version_and_expires_on_change(agent, database):
    client, headers, _ = agent
    _, _, user, _ = setup_organization(client, headers)
    with Session(database.engine) as session:
        before = session.get(User, int(user["id"])).password_hash
    conversation, run_id = action_run(
        agent,
        {
            "action": "open_admin_user_password_reset",
            "target": {"name": user["username"]},
        },
    )
    assert snapshot(agent, run_id)["status"] == "completed"
    part = answer(agent, conversation)["content_parts"][0]
    assert part == {
        "type": "client_action",
        "action": "open_admin_user_password_reset",
        "user_id": user["id"],
        "display_name": user["username"],
        "row_version": 1,
        "available": True,
    }
    with Session(database.engine) as session:
        assert session.get(User, int(user["id"])).password_hash == before
    assert (
        client.patch(
            U + "/" + user["id"],
            headers=headers,
            json={
                "row_version": 1,
                "username": "改名用户",
            },
        ).status_code
        == 200
    )
    assert answer(agent, conversation)["content_parts"] == [
        {
            "type": "client_action",
            "action": "open_admin_user_password_reset",
            "available": False,
        }
    ]


def test_historical_navigation_hidden_when_actor_loses_admin_role(agent, database):
    client, headers, _ = agent
    department, _, _, _ = setup_organization(client, headers)
    conversation, _ = action_run(agent, {"action": "open_admin_user_create"})
    with Session(database.engine) as session, session.begin():
        user = session.get(User, database.user_id())
        user.role, user.department_id, user.row_version = "user", int(department["id"]), 2
    assert answer(agent, conversation)["content_parts"] is None


def test_client_action_is_revalidated_before_answer_commit(agent, database, monkeypatch):
    client, headers, _ = agent
    _, _, user, _ = setup_organization(client, headers)
    original = Execution.persist_terminal

    async def changed(self, state):
        with Session(database.engine) as session, session.begin():
            row = session.get(User, int(user["id"]))
            row.row_version += 1
        return await original(self, state)

    monkeypatch.setattr(Execution, "persist_terminal", changed)
    conversation, run_id = action_run(
        agent,
        {
            "action": "open_admin_user_password_reset",
            "target": {"id": user["id"]},
        },
    )
    assert snapshot(agent, run_id)["failure_code"] == "ROW_VERSION_CONFLICT"
    assert answer(agent, conversation)["content_parts"] is None


def test_client_action_restart_keeps_only_control_state_in_checkpoint(agent, database, monkeypatch):
    client, _, manager = agent
    manager.model = ActionModel(
        {
            "action": "open_admin_user_create",
            "prefill": {"username": "恢复后创建"},
        }
    )
    conversation, run_id = new_run(agent)
    original = Execution.generate_answer

    async def crash():
        reached = asyncio.Event()

        async def pause(self, state):
            reached.set()
            await asyncio.Event().wait()
            return await original(self, state)

        monkeypatch.setattr(Execution, "generate_answer", pause)
        task = asyncio.create_task(manager.execute(run_id))
        await asyncio.wait_for(reached.wait(), 8)
        async with open_saver(manager.checkpoint_url) as saver:
            state = (
                await Execution(manager, run_id)
                .graph(saver)
                .aget_state(
                    {
                        "configurable": {"thread_id": f"agent-run:{run_id}"},
                    }
                )
            )
            assert state.values["client_action_pending"] is True
            assert "恢复后创建" not in json.dumps(state.values, ensure_ascii=False)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    client.portal.call(crash)
    assert answer(agent, conversation)["content_parts"] is None
    monkeypatch.setattr(Execution, "generate_answer", original)
    client.portal.call(manager.execute, run_id)
    assert snapshot(agent, run_id)["status"] == "completed"
    assert answer(agent, conversation)["content_parts"][0]["available"] is True
    with Session(database.engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Message).where(Message.role == "assistant")
            )
            == 1
        )
