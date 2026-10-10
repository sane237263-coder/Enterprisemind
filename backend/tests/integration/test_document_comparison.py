"""Opt-in real-model comparison and summary of synthetic authorized documents."""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_rag_pipeline import indexed, upload_index  # noqa: F401

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.agent.models import AgentRun, AnswerSource, Message, ToolCall
from enterprise_mind.app import create_app
from enterprise_mind.ingestion.models import Document
from enterprise_mind.rag.models import Chunk
from enterprise_mind.rag.repository import RetrievalRepository
from enterprise_mind.rag.retrieval import RetrievalService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EM_TEST_LIVE_DOCUMENT_COMPARE") != "1",
        reason="Real Qwen comparison requires explicit EM_TEST_LIVE_DOCUMENT_COMPARE=1",
    ),
]

SYNTHETIC_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_corpus"


@pytest.mark.parametrize(
    ("kind", "second_available"),
    [
        ("comparison", True),
        ("comparison", False),
        ("summary", True),
        ("summary", False),
        ("single-summary", False),
        ("synthesis", True),
        ("synthesis", False),
    ],
    ids=[
        "both-documents",
        "missing-document",
        "both-summary",
        "missing-summary",
        "single-summary",
        "synthesis-both",
        "synthesis-missing",
    ],
)
def test_live_qwen_uses_requested_document_sources(request, database, kind, second_available):
    """Only synthetic policy text is sent to the configured model."""
    assert os.getenv("EM_PROVIDERS_CONFIG")
    indexed_runtime = request.getfixturevalue("indexed")
    old = upload_index(
        indexed_runtime,
        name="travel-2025.md",
        body=(SYNTHETIC_CORPUS / "travel_2025.md").read_text(encoding="utf-8"),
    )
    expected_ids = {int(old["document_id"])}
    if second_available:
        new = upload_index(
            indexed_runtime,
            name="travel-2026.md",
            body=(SYNTHETIC_CORPUS / "travel_2026.md").read_text(encoding="utf-8"),
        )
        expected_ids.add(int(new["document_id"]))
    with Session(database.engine) as session:
        for document_id in expected_ids:
            document = session.get(Document, document_id)
            assert document is not None and document.active_revision_id is not None

    _, retrieval, _, embedding, reranker = indexed_runtime
    app = create_app(database.settings)
    manager = app.state.agent
    manager.start = lambda: None
    manager.retrieval = RetrievalService(
        RetrievalRepository(
            app.state.sessions, retrieval.config, embedding.model_key, embedding.dimensions
        ),
        embedding,
        reranker,
        retrieval.config,
    )
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        client.portal.call(initialize_checkpoints, database.url)
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "初始管理员", "password": database.admin_password},
        )
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["data"]["access_token"]}
        created = client.post(
            "/api/v1/conversations",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"title": "合成差旅政策对比"},
        )
        assert created.status_code == 201
        if kind == "comparison":
            question = (
                "对比文档 travel-2025.md 和 travel-2026.md 的报销期限与住宿上限，"
                "说明共同点和差异，分别引用两份文档的来源；不要猜测。"
            )
        elif kind == "summary":
            question = (
                "分别总结文档 travel-2025.md 和 travel-2026.md 的报销期限与住宿上限，"
                "保留各自数值并分别引用两份文档的来源；不要猜测。"
            )
        elif kind == "synthesis":
            question = (
                "综合星桥测试公司 2025 年和 2026 年差旅政策中的报销期限与住宿上限，"
                "先列可核实事实，再单列由这些事实推导的执行建议；分别引用依据，"
                "找不到某年的依据就明确说明，不要猜测。"
            )
        else:
            question = "总结文档 travel-2025.md 的报销期限与住宿上限并引用来源；不要猜测。"
        submitted = client.post(
            f"/api/v1/conversations/{created.json()['data']['id']}/messages",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content_text": question},
        )
        assert submitted.status_code == 202
        run_id = int(submitted.json()["data"]["id"])
        client.portal.call(manager.execute, run_id)
        with Session(database.engine) as session:
            run = session.get(AgentRun, run_id)
            assert run is not None and run.status == "completed", run.failure_code if run else None
            answer = session.get(Message, run.assistant_message_id)
            assert answer is not None and answer.content_text
            source_ids = set(
                session.scalars(
                    select(Chunk.document_id)
                    .join(AnswerSource, AnswerSource.chunk_id == Chunk.id)
                    .where(AnswerSource.message_id == answer.id)
                )
            )
            calls = list(
                session.scalars(
                    select(ToolCall).where(
                        ToolCall.agent_run_id == run_id,
                        ToolCall.tool_name == "knowledge_retrieve",
                    )
                )
            )
            if second_available:
                assert all(value in answer.content_text for value in ("17", "480", "21", "520")), {
                    "present": {
                        value: value in answer.content_text for value in ("17", "480", "21", "520")
                    },
                    "source_ids": sorted(source_ids),
                    "expected_ids": sorted(expected_ids),
                    "retrieval": [
                        {
                            "status": (call.result_summary or {}).get("status"),
                            "document_names": call.arguments.get("document_names"),
                            "document_ids": call.arguments.get("document_ids"),
                            "chunk_count": len((call.result_summary or {}).get("chunk_ids") or []),
                        }
                        for call in calls
                    ],
                }
                if kind == "synthesis":
                    assert "事实" in answer.content_text and "建议" in answer.content_text
            elif kind == "single-summary":
                assert "17" in answer.content_text and "480" in answer.content_text
                assert "21" not in answer.content_text and "520" not in answer.content_text
            elif kind == "synthesis":
                assert "21" not in answer.content_text and "520" not in answer.content_text
            else:
                assert "21" not in answer.content_text and "520" not in answer.content_text
                assert any(
                    marker in answer.content_text
                    for marker in ("缺少", "不足", "未找到", "无法", "不能", "未提供")
                )
            if second_available or kind == "single-summary":
                assert source_ids == expected_ids
            elif kind == "synthesis":
                assert source_ids <= expected_ids
            else:
                assert source_ids == set()
            assert calls and all(call.status == "success" for call in calls)
            if kind == "synthesis" and not second_available:
                if source_ids:
                    assert source_ids == expected_ids
                    assert "17" in answer.content_text and "480" in answer.content_text
                    assert "建议" in answer.content_text
                    assert any(
                        word in answer.content_text for word in ("缺少", "不足", "未找到", "无法")
                    )
                else:
                    assert "17" not in answer.content_text and "480" not in answer.content_text
                    assert all(
                        (call.result_summary or {}).get("status") == "insufficient_evidence"
                        for call in calls
                    ), [(call.result_summary or {}).get("status") for call in calls]
                    assert "没有找到足够依据" in answer.content_text
            names = {name for call in calls for name in call.arguments.get("document_names") or []}
            if kind == "synthesis":
                assert names <= {"travel-2025.md", "travel-2026.md"}
            else:
                assert names == (
                    {"travel-2025.md"}
                    if kind == "single-summary"
                    else {"travel-2025.md", "travel-2026.md"}
                )
