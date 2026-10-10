"""Opt-in, paid provider acceptance against disposable local infrastructure.

Requires EM_TEST_LIVE_MODELS=1 plus explicitly selected Provider/RAG configuration.
Only synthetic documents and questions below may be sent to configured providers.
No provider names, model IDs, endpoints or credentials are selected by this test.
"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from conftest import BACKEND, ROOT
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from enterprise_mind.agent.checkpoints import agent_event_loop, initialize_checkpoints
from enterprise_mind.agent.models import Message, ToolCall
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.infrastructure.database import create_engine, session_factory
from enterprise_mind.infrastructure.migrate import migrate
from enterprise_mind.infrastructure.provision import (
    APP,
    BACKUP,
    CHECKPOINT,
    MIGRATOR,
    OWNER,
    provision,
)
from enterprise_mind.infrastructure.settings import ApplicationConfig
from enterprise_mind.ingestion.models import Document
from enterprise_mind.rag.citations import citation_candidates, resolve_citation
from enterprise_mind.rag.config import RAGSettings, load_rag
from enterprise_mind.rag.models import Chunk, ChunkEmbedding
from enterprise_mind.rag.providers import HTTPEmbeddings, HTTPReranker, llm_token_counter
from enterprise_mind.rag.repository import RetrievalRepository
from enterprise_mind.rag.retrieval import RetrievalService
from enterprise_mind.rag.vector_index import build_vector_index, index_name

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EM_TEST_LIVE_MODELS") != "1",
        reason="Real model calls require explicit EM_TEST_LIVE_MODELS=1",
    ),
]

POLICY = "# 星桥测试公司差旅制度\n\n差旅报销必须在出差结束后 17 天内提交，每晚住宿上限为 480 元。"
PRIVATE = "# 暮云部门项目\n\n暮云部门内部项目代号为松林2719，预算为 8642 万元。"
QUESTION = "请检索知识库：星桥测试公司的差旅报销必须在出差结束后多少天内提交？请引用来源。"
UNKNOWN = "请检索公司知识库：公司 2027 年春节具体放假多少天？资料没有规定时请说明没有依据。"
FORBIDDEN = "请检索知识库：暮云部门内部项目代号和预算分别是什么？资料没有规定时请说明没有依据。"
ROLES = (OWNER, MIGRATOR, APP, BACKUP, CHECKPOINT)


def post(client, headers, path, body, status=201):
    response = client.post(path, headers={**headers, "Idempotency-Key": uuid4().hex}, json=body)
    assert response.status_code == status, f"Unexpected HTTP status: {response.status_code}"
    return response.json()["data"]


def login(client, username, password):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


@pytest.fixture
def live_runtime(database, ingestion_settings, tmp_path):
    assert os.getenv("EM_PROVIDERS_CONFIG") and os.getenv("EM_RAG_CONFIG")
    rag, providers = load_rag(RAGSettings())
    assert providers.llm and providers.embedding and providers.rerank
    services, _, _ = ingestion_settings
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "synthetic_backend_http_rag",
        "models": {
            name: getattr(providers, name).model_key for name in ("llm", "embedding", "rerank")
        },
        "checks": {},
        "complete": False,
    }
    report_path = Path(
        os.getenv("EM_LIVE_REPORT_PATH", str(ROOT / ".local/model-checks/rag-live.json"))
    )

    def record(name, **result):
        report["checks"][name] = result
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"live-rag: {name}: {result.get('status', 'recorded')}", flush=True)

    # This test must not adopt existing roles or use any configured application database.
    assert database.url.database.startswith("em_test_")
    with database.engine.connect() as connection:
        assert not connection.scalar(
            text("SELECT count(*) FROM pg_roles WHERE rolname=ANY(:roles)"), {"roles": list(ROLES)}
        )
    files, passwords = {}, {}
    for role in ROLES[1:]:
        passwords[role] = secrets.token_urlsafe(32)
        files[role] = tmp_path / role
        files[role].write_text(passwords[role], encoding="utf-8")
    process = None
    try:
        provision(database.url, files)
        migrator_url = database.url.set(username=MIGRATOR, password=passwords[MIGRATOR])
        migrate(migrator_url, ApplicationConfig(), BACKEND)
        build_vector_index(migrator_url, providers.embedding, rag.hnsw)
        with database.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname=:name"
                ),
                {"name": index_name(providers.embedding.model_key, providers.embedding.dimensions)},
            )
        record(
            "configured_vector_index", status="passed", dimensions=providers.embedding.dimensions
        )
        with asyncio.Runner(loop_factory=agent_event_loop) as runner:
            runner.run(initialize_checkpoints(migrator_url))
        settings = database.settings.model_copy(
            update={
                "env": "production",
                "public_origin": "https://enterprise-mind.test",
                "postgres_user": APP,
                "postgres_password_file": files[APP],
            }
        )
        env = os.environ.copy()
        for model in (settings, services):
            for field, value in model.model_dump().items():
                if value is not None:
                    env["EM_" + field.upper()] = str(value)
        env["EM_CHECKPOINT_POSTGRES_USER"] = CHECKPOINT
        env["EM_CHECKPOINT_POSTGRES_PASSWORD_FILE"] = str(files[CHECKPOINT])
        env["PYTHONUTF8"] = "1"
        env.pop("EM_BOOTSTRAP_ADMIN_USERNAME", None)
        env.pop("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", None)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import uvicorn; uvicorn.run('enterprise_mind.app:create_app', "
                f"factory=True, host='127.0.0.1', port={port}, access_log=False, log_config=None, "
                "loop='enterprise_mind.agent.checkpoints:agent_event_loop')",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
        ) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert process.poll() is None, "Isolated API exited during startup"
                try:
                    response = client.get("/health/ready")
                    if response.status_code == 200:
                        assert response.json()["stage"] == "ingestion_foundation"
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
            else:
                pytest.fail("Isolated API startup timed out")
            record("api_and_checkpoint", status="passed", dedicated_roles=True)
            headers = login(client, "初始管理员", database.admin_password)
            yield client, headers, env, settings, rag, providers, report, record
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        with database.engine.begin() as connection:
            if connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": OWNER}):
                connection.execute(text(f'REASSIGN OWNED BY {OWNER} TO "em_test_owner"'))
            for role in (CHECKPOINT, APP, BACKUP, MIGRATOR, OWNER):
                if connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": role}):
                    connection.execute(text(f"DROP OWNED BY {role}"))
                    connection.execute(text(f"DROP ROLE {role}"))
        report["finished_at"] = datetime.now(UTC).isoformat()
        record("runtime_cleanup", status="passed")


def test_live_upload_retrieval_answer_citations_and_permissions(live_runtime, database):
    client, admin, env, settings, rag, providers, report, record = live_runtime
    department = post(client, admin, "/api/v1/admin/departments", {"name": "暮云测试部门"})
    public = post(client, admin, "/api/v1/knowledge-bases", {"name": "公开测试", "type": "public"})
    private = post(
        client,
        admin,
        "/api/v1/knowledge-bases",
        {"name": "部门测试", "type": "department", "department_id": department["id"]},
    )
    outsider_department = post(client, admin, "/api/v1/admin/departments", {"name": "外部测试部门"})
    user_password = secrets.token_urlsafe(24)
    user = post(
        client,
        admin,
        "/api/v1/admin/users",
        {
            "username": "外部测试成员",
            "password": user_password,
            "role": "user",
            "department_id": outsider_department["id"],
        },
    )
    user_headers = login(client, "外部测试成员", user_password)
    documents = []
    for base, name, content in ((public, "差旅制度.md", POLICY), (private, "项目资料.md", PRIVATE)):
        response = client.post(
            f"/api/v1/knowledge-bases/{base['id']}/document-uploads",
            headers={**admin, "Idempotency-Key": uuid4().hex},
            files=[("files", (name, content.encode(), "text/markdown"))],
        )
        assert response.status_code == 202
        documents.append(int(response.json()["data"]["items"][0]["document_id"]))
    record("upload", status="passed", document_count=len(documents))
    for command in ("dispatch", "worker", "worker"):
        completed = subprocess.run(
            [sys.executable, "-m", "enterprise_mind.cli", command, "--once"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=240,
        )
        assert completed.returncode == 0, f"Isolated {command} failed"
    with Session(database.engine) as session:
        rows = session.scalars(select(Document)).all()
        assert len(rows) == 2
        assert all(row.processing_status == "success" and row.active_revision_id for row in rows)
        chunks = session.scalar(select(func.count()).select_from(Chunk))
        vectors = session.scalar(select(func.count()).select_from(ChunkEmbedding))
        assert chunks >= 2 and vectors == chunks
        assert set(
            session.execute(
                select(ChunkEmbedding.model_key, ChunkEmbedding.dimensions, ChunkEmbedding.status)
            ).all()
        ) == {(providers.embedding.model_key, providers.embedding.dimensions, "ready")}
    for document_id in documents:
        progress = client.get(f"/api/v1/documents/{document_id}/processing", headers=admin)
        assert (
            progress.status_code == 200 and progress.json()["data"]["current_stage"] == "activate"
        )
    record("parse_chunk_embed_activate", status="passed", chunks=chunks, vectors=vectors)
    assert (
        client.get(f"/api/v1/documents/{documents[1]}/preview", headers=user_headers).status_code
        == 404
    )
    record("unauthorized_document", status="passed")

    async def retrieval_checks():
        engine = create_engine(settings, ApplicationConfig())
        sessions = session_factory(engine)
        embedding, reranker = HTTPEmbeddings(providers.embedding), HTTPReranker(providers.rerank)
        counter = llm_token_counter(providers.llm)
        service = RetrievalService(
            RetrievalRepository(sessions, rag, embedding.model_key, embedding.dimensions),
            embedding,
            reranker,
            rag,
        )
        try:
            async with sessions() as session:
                member = await session.get(User, int(user["id"]))
                member_department = await session.get(Department, member.department_id)
                identity = CurrentIdentity(member, member_department, member.credential_version)
                root = await session.get(User, database.user_id())
                root_identity = CurrentIdentity(root, None, root.credential_version)
                # Confirm the private positive control exists, rather than testing an empty index.
                assert await session.get(Department, int(department["id"]))
            for name, question, actor, expected in (
                ("retrieval_public", QUESTION, identity, "ok"),
                (
                    "retrieval_english",
                    "How many days after a business trip must travel reimbursement be submitted?",
                    identity,
                    "ok",
                ),
                (
                    "retrieval_mixed",
                    "星桥测试公司 travel reimbursement: how many days after the trip?",
                    identity,
                    "ok",
                ),
                ("retrieval_unknown", UNKNOWN, identity, "insufficient_evidence"),
                ("retrieval_private_control", FORBIDDEN, root_identity, "ok"),
                ("retrieval_permission_filter", FORBIDDEN, identity, "insufficient_evidence"),
            ):
                result = await service.retrieve(
                    actor, question, available_input_tokens=12000, counter=counter
                )
                assert result.status == expected, f"{name}: {result.status}"
                assert not result.warnings, f"{name}: provider fallback occurred"
                ids = {
                    candidate.document_id
                    for context in result.contexts
                    for candidate in context.candidates
                }
                if actor is identity:
                    assert documents[1] not in ids
                record(name, status="passed", result=result.status, contexts=len(result.contexts))
        finally:
            await engine.dispose()

    with asyncio.Runner(loop_factory=agent_event_loop) as runner:
        runner.run(retrieval_checks())

    failures = []
    positive_source = None
    positive_conversation = None
    last_run_finished = None
    for name, question, expected_answer in (
        ("answer_with_citation", QUESTION, True),
        ("answer_unknown", UNKNOWN, False),
        ("answer_permission_filter", FORBIDDEN, False),
    ):
        # Pace the small live acceptance sample; do not alter application retry settings.
        if last_run_finished is not None:
            time.sleep(max(0, 45 - (time.monotonic() - last_run_finished)))
        conversation = post(client, user_headers, "/api/v1/conversations", {"title": name})
        run = post(
            client,
            user_headers,
            f"/api/v1/conversations/{conversation['id']}/messages",
            {"content_text": question},
            status=202,
        )
        event_names, streamed_text = [], []
        with client.stream(
            "GET", f"/api/v1/agent-runs/{run['id']}/events", headers=user_headers, timeout=660
        ) as response:
            assert response.status_code == 200
            event = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                    event_names.append(event)
                elif line.startswith("data: ") and event == "answer.delta":
                    streamed_text.append(json.loads(line[6:])["text"])
        response = client.get(f"/api/v1/agent-runs/{run['id']}", headers=user_headers)
        assert response.status_code == 200
        state = response.json()["data"]
        last_run_finished = time.monotonic()
        if state["status"] != "completed":
            failures.append(name)
            record(name, status="failed", failure_code=state["failure_code"], events=event_names)
            assert "run.completed" not in event_names
            assert "run.failed" in event_names
            continue
        history = client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=user_headers
        ).json()["data"]
        answer = next(item for item in history if item["id"] == state["assistant_message_id"])
        assert answer["status"] == "completed"
        assert "run.completed" in event_names
        # SSE is ephemeral; reconnect/terminal snapshots are reconciled with persisted history.
        assert answer["content_text"].startswith("".join(streamed_text))
        with Session(database.engine) as session:
            calls = session.scalars(
                select(ToolCall).where(ToolCall.agent_run_id == int(run["id"]))
            ).all()
            assert calls and all(
                call.tool_name == "knowledge_retrieve" and call.status == "success"
                for call in calls
            )
        if expected_answer:
            assert streamed_text
            assert "17" in answer["content_text"] and "[1]" in answer["content_text"]
            assert "[[source_" not in answer["content_text"]
            assert answer["sources"]
            for source in answer["sources"]:
                assert source["document_id"] == str(documents[0]) and source["available"]
                assert source["quote_text"] in POLICY
                preview = client.get(
                    f"/api/v1/answer-sources/{source['id']}/preview", headers=user_headers
                )
                assert preview.status_code == 200
                data = preview.json()["data"]
                assert data["preview"]["highlight"]["locator"]
                for artifact in data["preview"]["artifacts"]:
                    content = client.get(
                        artifact["content_url"].split("#")[0], headers=user_headers
                    )
                    assert content.status_code == 200 and content.content
                assert (
                    client.get(
                        f"/api/v1/answer-sources/{source['id']}/preview", headers=admin
                    ).status_code
                    == 404
                )
            positive_source = answer["sources"][0]["id"]
            positive_conversation = conversation["id"]
        else:
            assert "没有找到足够依据" in answer["content_text"] and not answer["sources"]
            assert "2719" not in answer["content_text"] and "8642" not in answer["content_text"]
        record(
            name,
            status="passed",
            answer=answer["content_text"],
            source_count=len(answer["sources"]),
            delta_count=len(streamed_text),
        )
    if positive_source:
        # Revoke the member through the management API and verify old citations close.
        response = client.patch(
            f"/api/v1/admin/users/{user['id']}",
            headers=admin,
            json={"row_version": user["row_version"], "status": "disabled"},
        )
        assert response.status_code == 200
        for path in (
            f"/api/v1/answer-sources/{positive_source}/preview",
            f"/api/v1/conversations/{positive_conversation}/messages",
        ):
            assert client.get(path, headers=user_headers).status_code in (401, 403)
        record("permission_revocation", status="passed")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with database.engine.connect() as connection:
            remaining = connection.scalar(
                text("SELECT count(*) FROM langgraph_checkpoint.checkpoints")
            )
        if not remaining:
            break
        time.sleep(0.1)
    assert remaining == 0
    with Session(database.engine) as session:
        assert not session.scalar(select(Message.id).where(Message.status == "streaming").limit(1))
    record("terminal_checkpoint_cleanup", status="passed")
    report["complete"] = not failures
    assert not failures, "Live model scenarios failed; see sanitized local report"


def test_live_synthetic_policy_corpus_ranks_the_requested_document(live_runtime, database):
    client, admin, env, settings, rag, providers, report, record = live_runtime
    base = post(client, admin, "/api/v1/knowledge-bases", {"name": "合成政策库", "type": "public"})
    corpus = ROOT / "backend" / "tests" / "fixtures" / "synthetic_corpus"
    filenames = (
        "travel_2025.md",
        "travel_2026.md",
        "procurement_2026.md",
        "incident_2026.md",
    )
    document_ids = {}
    for filename in filenames:
        response = client.post(
            f"/api/v1/knowledge-bases/{base['id']}/document-uploads",
            headers={**admin, "Idempotency-Key": uuid4().hex},
            files=[
                (
                    "files",
                    (
                        filename,
                        (corpus / filename).read_bytes(),
                        "text/markdown",
                    ),
                )
            ],
        )
        assert response.status_code == 202
        document_ids[filename] = int(response.json()["data"]["items"][0]["document_id"])
    for command in ("dispatch", *("worker" for _ in filenames)):
        completed = subprocess.run(
            [sys.executable, "-m", "enterprise_mind.cli", command, "--once"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=240,
        )
        assert completed.returncode == 0, f"Isolated {command} failed"
    with Session(database.engine) as session:
        assert all(
            session.get(Document, document_id).processing_status == "success"
            for document_id in document_ids.values()
        )

    async def check_retrieval():
        engine = create_engine(settings, ApplicationConfig())
        sessions = session_factory(engine)
        embedding, reranker = HTTPEmbeddings(providers.embedding), HTTPReranker(providers.rerank)
        service = RetrievalService(
            RetrievalRepository(sessions, rag, embedding.model_key, embedding.dimensions),
            embedding,
            reranker,
            rag,
        )
        try:
            async with sessions() as session:
                root = await session.get(User, database.user_id())
                identity = CurrentIdentity(root, None, root.credential_version)
            for name, question in (
                ("travel_2025.md", "2025 年差旅报销期限与住宿上限是多少？"),
                (
                    "travel_2026.md",
                    "What is the 2026 travel reimbursement deadline and hotel cap?",
                ),
                ("procurement_2026.md", "5000 元采购申请需要谁审批？"),
                ("incident_2026.md", "客户数据疑似泄露后应在几小时内报告？"),
            ):
                result = await service.retrieve(
                    identity,
                    question,
                    available_input_tokens=12000,
                    counter=llm_token_counter(providers.llm),
                )
                assert result.status == "ok" and result.contexts, (name, result.status)
                assert not result.warnings
                first = result.contexts[0].candidates[0]
                assert first.document_id == document_ids[name], name
                choices = citation_candidates(result)
                chosen = next(item for item in choices.values() if item.chunk_id == first.id)
                citation = await resolve_citation(
                    service.repository, identity, choices, chosen.key, chosen.quote
                )
                assert citation.document_id == document_ids[name]
                record(name, status="passed", top_document=name, contexts=len(result.contexts))
        finally:
            await engine.dispose()

    with asyncio.Runner(loop_factory=agent_event_loop) as runner:
        runner.run(check_retrieval())
    conversation = post(client, admin, "/api/v1/conversations", {"title": "合成差旅政策跨版本对比"})
    run = post(
        client,
        admin,
        f"/api/v1/conversations/{conversation['id']}/messages",
        {
            "content_text": (
                "对比文档 travel_2025.md 和 travel_2026.md 的报销期限与住宿上限，"
                "分别引用两份资料；不要引用采购或安全事件资料。"
            )
        },
        status=202,
    )
    with client.stream(
        "GET", f"/api/v1/agent-runs/{run['id']}/events", headers=admin, timeout=660
    ) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass
    response = client.get(f"/api/v1/agent-runs/{run['id']}", headers=admin)
    assert response.status_code == 200
    state = response.json()["data"]
    assert state["status"] == "completed", state["failure_code"]
    history = client.get(
        f"/api/v1/conversations/{conversation['id']}/messages", headers=admin
    ).json()["data"]
    answer = next(item for item in history if item["id"] == state["assistant_message_id"])
    assert all(value in answer["content_text"] for value in ("17", "480", "21", "520"))
    assert {source["document_id"] for source in answer["sources"]} == {
        str(document_ids["travel_2025.md"]),
        str(document_ids["travel_2026.md"]),
    }
    with Session(database.engine) as session:
        calls = session.scalars(
            select(ToolCall).where(
                ToolCall.agent_run_id == int(run["id"]),
                ToolCall.tool_name == "knowledge_retrieve",
            )
        ).all()
        assert calls and all(call.status == "success" for call in calls)
    record("agent_comparison", status="passed", source_count=len(answer["sources"]))
    report["complete"] = True
