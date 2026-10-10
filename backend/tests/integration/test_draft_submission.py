"""Draft submission binds retries to the authorized conversation, without a model."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_management import auth

from enterprise_mind.agent.generation_models import ContentDraft
from enterprise_mind.agent.models import Conversation
from enterprise_mind.app import create_app
from enterprise_mind.generation import service

pytestmark = pytest.mark.integration
BODY = {"title": "Core retry regression", "body_markdown": "Synthetic draft content.", "tables": []}


@pytest.fixture
def draft_api(database):
    database.initialize()
    app = create_app(database.settings)
    with TestClient(app) as client:
        headers = auth(client, "初始管理员", database.admin_password)
        conversations = []
        for key in ["first", "second"]:
            response = client.post(
                "/api/v1/conversations",
                headers={**headers, "Idempotency-Key": "draft-scope-conversation-" + key},
                json={"title": key},
            )
            assert response.status_code == 201
            conversations.append(response.json()["data"]["id"])
        yield client, headers, conversations


def submit(client, headers, conversation_id, key="draft-retry"):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/drafts",
        headers={**headers, "Idempotency-Key": key},
        json=BODY,
    )


def test_draft_retry_cannot_return_a_different_conversations_draft(draft_api, database):
    client, headers, (first, second) = draft_api
    original = submit(client, headers, first)
    repeated = submit(client, headers, first)
    assert original.status_code == repeated.status_code == 201
    assert original.json()["data"]["id"] == repeated.json()["data"]["id"]
    wrong_target = submit(client, headers, second)
    assert wrong_target.status_code == 409, wrong_target.text
    assert wrong_target.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ContentDraft)) == 1
    separate = submit(client, headers, second, "second-draft-key")
    assert separate.status_code == 201
    assert separate.json()["data"]["conversation_id"] == second


def test_draft_replay_rechecks_deleted_conversation(draft_api, database):
    client, headers, (first, _) = draft_api
    assert submit(client, headers, first).status_code == 201
    with Session(database.engine) as session, session.begin():
        # The database's existing cascade applies, as for completed conversation deletion.
        item = session.get(Conversation, int(first))
        session.delete(item)
    replay = submit(client, headers, first)
    assert replay.status_code == 404, replay.text


def test_draft_replay_rechecks_conversation_ownership(draft_api):
    client, headers, (first, _) = draft_api
    assert submit(client, headers, first).status_code == 201
    created = client.post(
        "/api/v1/admin/users",
        headers={**headers, "Idempotency-Key": "draft-other-user"},
        json={
            "username": "draft-other-owner",
            "password": "Core-Draft-Owner-2026!",
            "role": "admin",
        },
    )
    assert created.status_code == 201
    other = auth(client, "draft-other-owner", "Core-Draft-Owner-2026!")
    target = client.post(
        "/api/v1/conversations",
        headers={**other, "Idempotency-Key": "other-conversation"},
        json={"title": "Other user's conversation"},
    )
    assert target.status_code == 201
    replay = submit(client, headers, target.json()["data"]["id"])
    assert replay.status_code == 404, replay.text


def test_concurrent_draft_retries_create_one_draft(draft_api, database, monkeypatch):
    client, headers, (first, _) = draft_api
    create = service.create_draft

    async def delayed(*args, **kwargs):
        # Widen the window after the idempotency lookup, without mocking persistence.
        await asyncio.sleep(0.15)
        return await create(*args, **kwargs)

    monkeypatch.setattr(service, "create_draft", delayed)
    barrier = threading.Barrier(2)

    def request():
        barrier.wait(timeout=5)
        return submit(client, headers, first)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        responses = [future.result(timeout=15) for future in futures]
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["data"]["id"] == responses[1].json()["data"]["id"]
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ContentDraft)) == 1
