import asyncio
import json
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_rag_pipeline import indexed, upload_index  # noqa: F401

from enterprise_mind.agent.checkpoints import initialize_checkpoints
from enterprise_mind.agent.models import AgentRun, Message
from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.agent.translation import TranslationUnavailable, prepare_translation
from enterprise_mind.app import create_app
from enterprise_mind.ingestion.models import Document, ProcessingTask
from enterprise_mind.rag.citations import resolve_citation
from enterprise_mind.rag.config import RAGSettings, load_rag
from enterprise_mind.rag.repository import RetrievalRepository, Scope
from enterprise_mind.rag.retrieval import RetrievalService

pytestmark = pytest.mark.integration


def _assert_yuan_currency(answer: str, amount: int) -> None:
    normalized = answer.replace(",", "").replace("，", "")
    assert re.search(
        rf"(?<!\d){amount}(?:\.0+)?\s*(?:yuan\b|renminbi\b|RMB\b|CNY\b|元)"
        rf"|(?:\bRMB\b|\bCNY\b|人民币|[¥￥])\s*{amount}(?:\.0+)?(?!\d)",
        normalized,
        re.IGNORECASE,
    ), f"Missing original yuan currency for amount {amount}"
    assert not re.search(
        rf"(?:(?:US\s*)?\$|\bUSD\b|美元)\s*{amount}(?:\.0+)?(?!\d)"
        rf"|(?<!\d){amount}(?:\.0+)?\s*(?:\bUSD\b|(?:US\s+)?dollars?\b|美元)",
        normalized,
        re.IGNORECASE,
    ), f"Original yuan amount {amount} was changed to dollars"


def test_full_and_chapter_translation_source_coverage(request, database):
    indexed_runtime = request.getfixturevalue("indexed")
    body = "# 第一章 概述\n\n第一章唯一内容。\n\n# 第二章 数据\n\n" + "\n\n".join(
        f"第二章原文序号 {number}：企业数据只用于授权翻译。" * 4 for number in range(18)
    )
    item = upload_index(indexed_runtime, name="guide.md", body=body)
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document is not None and document.active_revision_id is not None, (
            document.processing_status if document else None,
            [(task.status, task.error_code) for task in session.query(ProcessingTask)],
        )
    runtime, retrieval, identity, *_ = indexed_runtime
    scope = Scope(document_ids=(int(item["document_id"]),))

    async def verify():
        full = await prepare_translation(
            retrieval.repository, identity, scope, "把 guide.md 全文翻译成英文"
        )
        chapter = await prepare_translation(
            retrieval.repository, identity, scope, "把 guide.md 第二章翻译成英文"
        )
        assert full.title == "guide.md" and full.target_language == "英文"
        assert len(full.parts) > 1
        full_text = "\n".join(part.text for part in full.parts)
        assert "第一章唯一内容" in full_text
        assert all(f"第二章原文序号 {number}" in full_text for number in range(18))
        chapter_text = "\n".join(part.text for part in chapter.parts)
        assert "第一章唯一内容" not in chapter_text
        assert all(f"第二章原文序号 {number}" in chapter_text for number in range(18))
        for part in full.parts:
            assert part.citation is not None
            await resolve_citation(
                retrieval.repository,
                identity,
                {part.citation.key: part.citation},
                part.citation.key,
                part.citation.quote,
                scope=scope,
            )
        with pytest.raises(TranslationUnavailable, match="文档不存在"):
            await prepare_translation(
                retrieval.repository,
                identity,
                Scope(document_ids=(int(item["document_id"]) + 9999,)),
                "全文翻译成英文",
            )

    runtime.run(verify)


@pytest.mark.parametrize(
    "document_name", ["agent-guide.md", "agent-Chinese-guide.md", "差旅制度.md"]
)
def test_agent_translates_each_source_part_with_current_citations(request, database, document_name):
    indexed_runtime = request.getfixturevalue("indexed")
    body = (
        "# 第一章 概述\n\n"
        + "\n\n".join(f"原文标记 {number}：需逐段处理。" * 6 for number in range(15))
        + "\n\n# 第二章 结论\n\n第二章独有内容。星桥测试公司与 Cedar Analytics Ltd 执行本规定。\n\n"
        + "\n\n".join(
            f"第二章段落标记 {number}：所有合同记录都应保留完整审批与凭证。" * 4
            for number in range(16)
        )
    )
    item = upload_index(indexed_runtime, name=document_name, body=body)
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document is not None and document.active_revision_id is not None, (
            document.processing_status if document else None,
            [(task.status, task.error_code) for task in session.query(ProcessingTask)],
        )
    _, retrieval, _, embedding, reranker = indexed_runtime

    class Counter:
        def count(self, value):
            return len(value)

    class ControlledModel:
        counter = Counter()
        max_input_tokens = 32000
        max_output_tokens = 4096
        corrupt = False
        corrupt_calls = 0
        corrupt_once = False
        target_calls = 0
        corrupt_summary = False
        corrupt_name = False
        corrupt_extraction = False
        corrupt_summary_marker = False

        def __init__(self):
            self.timeline = []

        async def plan(self, messages, tools, feedback):
            raise AssertionError("An explicit translation must not depend on model planning")

        async def answer(self, messages, *, json_mode=False):
            payload = json.loads(messages[-1].content)
            assert json_mode == ("source_parts" not in payload)
            if "source_text" in payload and "target_language" not in payload:
                self.timeline.append(("model", "terms"))
                name = "伪造公司" if self.corrupt_extraction else "星桥测试公司"
                yield json.dumps(
                    {"terms": [{"text": name, "kind": "organization"}]}
                    if self.corrupt_extraction or name in payload["source_text"]
                    else {"terms": []},
                    ensure_ascii=False,
                )
                return
            if "source_lines" in payload:
                self.timeline.append(("model", "part", payload["source_label"]))
                if any("原文标记 14" in line for line in payload["source_lines"]):
                    self.target_calls += 1
                # The extractor intentionally omitted this English organization;
                # the source-only corporate-name backstop must still protect it.
                assert not any("Cedar Analytics Ltd" in line for line in payload["source_lines"])
                lines = []
                for source in payload["source_lines"]:
                    if (
                        self.corrupt or (self.corrupt_once and self.corrupt_calls == 0)
                    ) and "原文标记 14" in source:
                        self.corrupt_calls += 1
                        lines.append("Equipment")
                        continue
                    if self.corrupt_name:
                        source = re.sub(r"⟦EM_TERM_[^⟧]+⟧", "Starbridge Testing Company", source)
                    lines.append("Translated: " + source)
                yield json.dumps({"lines": lines}, ensure_ascii=False)
            else:
                self.timeline.append(("model", "summary"))
                source = "\n".join(payload["source_parts"])
                assert "第二章独有内容" in source
                assert "原文标记 0" not in source
                assert "Translated:" not in source
                if self.corrupt_summary_marker:
                    yield f"Summary ⟦EM_TERM_{'0' * 32}_999⟧."
                else:
                    yield "Summary $900." if self.corrupt_summary else "Summary of translation."

        async def summarize(self, messages):
            return ""

        async def close(self):
            pass

    app = create_app(database.settings)
    manager = app.state.agent
    manager.start = lambda: None
    manager.model = ControlledModel()
    timeline = manager.model.timeline
    events = []
    publish = manager.bus.publish

    async def capture(run_id, event, data):
        text = data.get("text") if isinstance(data, dict) else None
        await publish(run_id, event, data)
        events.append((event, text))
        timeline.append(("event", event, text))

    manager.bus.publish = capture
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
        headers = {"Authorization": "Bearer " + login.json()["data"]["access_token"]}
        created = client.post(
            "/api/v1/conversations",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"title": "文档翻译"},
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["data"]["id"]
        submitted = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content_text": f"把 {document_name} 全文翻译成英文"},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = int(submitted.json()["data"]["id"])
        client.portal.call(manager.execute, run_id)
        with Session(database.engine) as session:
            run = session.get(AgentRun, run_id)
            assert run is not None and run.status == "completed", run.failure_code if run else None
            answer = session.get(Message, run.assistant_message_id)
            assert answer is not None and answer.content_text is not None
            assert all(f"原文标记 {number}" in answer.content_text for number in range(15))
            assert "Translated:" in answer.content_text and "[1]" in answer.content_text
            assert "星桥测试公司" in answer.content_text
            assert "Cedar Analytics Ltd" in answer.content_text
            assert "EM_TERM" not in answer.content_text
        events.clear()
        timeline.clear()
        chapter = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content_text": f"把 {document_name} 第二章翻译成英文并总结"},
        )
        assert chapter.status_code == 202, chapter.text
        chapter_run_id = int(chapter.json()["data"]["id"])
        client.portal.call(manager.execute, chapter_run_id)
        with Session(database.engine) as session:
            chapter_run = session.get(AgentRun, chapter_run_id)
            assert chapter_run is not None and chapter_run.status == "completed", (
                chapter_run.failure_code if chapter_run else None
            )
            chapter_answer = session.get(Message, chapter_run.assistant_message_id)
            assert chapter_answer is not None and chapter_answer.content_text is not None
            assert "第二章独有内容" in chapter_answer.content_text
            assert "原文标记 0" not in chapter_answer.content_text
            assert "Summary of translation." in chapter_answer.content_text

        part_starts = [
            (index, entry[2])
            for index, entry in enumerate(timeline)
            if entry[0] == "model" and entry[1] == "part"
        ]
        summary_start = next(
            index
            for index, entry in enumerate(timeline)
            if entry[0] == "model" and entry[1] == "summary"
        )
        delta_events = [
            (index, entry[2])
            for index, entry in enumerate(timeline)
            if entry[0] == "event" and entry[1] == "answer.delta"
        ]
        part_deltas = [(index, text) for index, text in delta_events if index < summary_start]
        summary_deltas = [(index, text) for index, text in delta_events if index > summary_start]
        assert len(part_starts) >= 2
        assert len(part_deltas) == len(part_starts)
        for index, ((part_start, _), (part_delta, _)) in enumerate(
            zip(part_starts, part_deltas, strict=True)
        ):
            assert part_start < part_delta
            if index + 1 < len(part_starts):
                assert part_delta < part_starts[index + 1][0]
        assert part_deltas[-1][0] < summary_start
        assert len(summary_deltas) == 1 and "### 译后总结" in summary_deltas[0][1]
        assert all(
            "EM_TERM" not in text and not text.lstrip().startswith("{") for _, text in delta_events
        )

        events.clear()
        timeline.clear()
        manager.model.corrupt = True
        manager.model.target_calls = 0
        rejected = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content_text": f"把 {document_name} 全文翻译成英文"},
        )
        assert rejected.status_code == 202
        rejected_run_id = int(rejected.json()["data"]["id"])
        client.portal.call(manager.execute, rejected_run_id)
        with Session(database.engine) as session:
            rejected_run = session.get(AgentRun, rejected_run_id)
            assert rejected_run is not None and rejected_run.status == "failed"
            assert rejected_run.failure_code == "ANSWER_SOURCE_INVALID"
            rejected_answer = session.get(Message, rejected_run.assistant_message_id)
            assert rejected_answer is not None and rejected_answer.status == "failed"
            assert "Equipment" not in (rejected_answer.content_text or "")
            assert "Translated:" not in (rejected_answer.content_text or "")
        assert manager.model.corrupt_calls == 2 and manager.model.target_calls == 2
        rejected_deltas = [text for event, text in events if event == "answer.delta"]
        # A rejection in the first part correctly produces no deltas at all.
        assert all("Equipment" not in text for text in rejected_deltas)
        assert all(
            "EM_TERM" not in text and not text.lstrip().startswith("{") for text in rejected_deltas
        )
        assert "run.failed" in [event for event, _ in events]
        assert "run.completed" not in [event for event, _ in events]

        events.clear()
        timeline.clear()
        manager.model.corrupt = False
        manager.model.corrupt_once = True
        manager.model.corrupt_calls = manager.model.target_calls = 0
        recovered = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content_text": f"把 {document_name} 全文翻译成英文"},
        )
        assert recovered.status_code == 202
        recovery_id = int(recovered.json()["data"]["id"])
        client.portal.call(manager.execute, recovery_id)
        with Session(database.engine) as session:
            recovery_run = session.get(AgentRun, recovery_id)
            assert recovery_run is not None and recovery_run.status == "completed"
            recovery_answer = session.get(Message, recovery_run.assistant_message_id)
            assert recovery_answer is not None and recovery_answer.content_text
            assert all(f"原文标记 {n}" in recovery_answer.content_text for n in range(15))
            assert "Cedar Analytics Ltd" in recovery_answer.content_text
        assert manager.model.corrupt_calls == 1 and manager.model.target_calls == 2
        recovery_deltas = [text for event, text in events if event == "answer.delta"]
        recovery_parts = [entry for entry in timeline if entry[0] == "model" and entry[1] == "part"]
        assert len(recovery_parts) == len(recovery_deltas) + 1
        assert len(recovery_deltas) > 1
        assert "run.failed" not in [event for event, _ in events]
        manager.model.corrupt_once = False
        events.clear()
        timeline.clear()
        manager.model.corrupt_summary = True
        rejected_summary = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content_text": f"把 {document_name} 第二章翻译成英文并总结"},
        )
        assert rejected_summary.status_code == 202
        summary_run_id = int(rejected_summary.json()["data"]["id"])
        client.portal.call(manager.execute, summary_run_id)
        with Session(database.engine) as session:
            summary_run = session.get(AgentRun, summary_run_id)
            assert summary_run is not None and summary_run.status == "failed"
            assert summary_run.failure_code == "ANSWER_SOURCE_INVALID"
            summary_message = session.get(Message, summary_run.assistant_message_id)
            assert summary_message is not None and summary_message.status == "failed"
            assert "$900" not in (summary_message.content_text or "")
        summary_deltas = [text for event, text in events if event == "answer.delta"]
        assert summary_deltas and all("$900" not in text for text in summary_deltas)
        assert all("译后总结" not in text for text in summary_deltas)
        assert "run.failed" in [event for event, _ in events]
        assert "run.completed" not in [event for event, _ in events]

        manager.model.corrupt_summary = False
        for fault in (
            "corrupt_extraction",
            "corrupt_name",
            "corrupt_summary_marker",
        ):
            events.clear()
            timeline.clear()
            setattr(manager.model, fault, True)
            # Valid earlier parts may stream, while the rejected part or summary must not.
            question = f"把 {document_name} 全文翻译成英文"
            if fault == "corrupt_summary_marker":
                question = f"把 {document_name} 第二章翻译成英文并总结"
            rejected_terms = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                json={"content_text": question},
            )
            assert rejected_terms.status_code == 202
            terms_run_id = int(rejected_terms.json()["data"]["id"])
            client.portal.call(manager.execute, terms_run_id)
            with Session(database.engine) as session:
                terms_run = session.get(AgentRun, terms_run_id)
                assert terms_run is not None and terms_run.status == "failed", fault
                assert terms_run.failure_code == "ANSWER_SOURCE_INVALID", fault
                terms_message = session.get(Message, terms_run.assistant_message_id)
                assert terms_message is not None and terms_message.status == "failed"
                assert not any(
                    marker in (terms_message.content_text or "")
                    for marker in ("Translated:", "Starbridge", "EM_TERM", "伪造公司")
                )
            fault_deltas = [text for event, text in events if event == "answer.delta"]
            if fault == "corrupt_extraction":
                assert not fault_deltas, fault
            elif fault == "corrupt_summary_marker":
                assert fault_deltas, fault
            assert all(
                not any(marker in text for marker in ("Starbridge", "EM_TERM", "伪造公司"))
                and not text.lstrip().startswith("{")
                for text in fault_deltas
            ), fault
            assert "run.failed" in [event for event, _ in events], fault
            assert "run.completed" not in [event for event, _ in events], fault
            setattr(manager.model, fault, False)


@pytest.mark.skipif(
    os.getenv("EM_TEST_LIVE_TRANSLATION") != "1",
    reason="Real Qwen translation requires explicit EM_TEST_LIVE_TRANSLATION=1",
)
@pytest.mark.parametrize("translation_scope", ["full", "chapter_summary"])
def test_live_qwen_document_and_chapter_translation(request, database, translation_scope):
    """Only synthetic source text is sent to the explicitly configured provider."""
    assert os.getenv("EM_PROVIDERS_CONFIG")
    _, providers = load_rag(RAGSettings())
    assert providers.llm is not None
    indexed_runtime = request.getfixturevalue("indexed")
    body = (
        "# 第一章 差旅\n\n"
        "星桥测试公司的差旅报销须在行程结束后 17 天内提交。"
        "住宿上限为每晚 480 元。\n\n"
        + "\n".join(
            f"差旅登记项 {number}：每项须保留日期和原始编号。" for number in range(1101, 1131)
        )
        + "\n\n"
        "# 第二章 设备\n\n"
        "星桥测试公司的设备申领须在 42 天内完成。"
        "每台设备的采购上限为 900 元。\n\n"
        + "\n".join(
            f"设备登记项 {number}：每项须保留名称和采购凭证。" for number in range(2201, 2231)
        )
    )
    upload_index(indexed_runtime, name="translation-live.md", body=body)
    _, retrieval, _, embedding, reranker = indexed_runtime
    app = create_app(database.settings)
    manager = app.state.agent
    manager.start = lambda: None
    manager.model = ChatModel(providers.llm)
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
            json={"title": "合成文档真实模型翻译"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["data"]["id"]

        def translate(question):
            submitted = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                json={"content_text": question},
            )
            assert submitted.status_code == 202
            run_id = int(submitted.json()["data"]["id"])
            client.portal.call(manager.execute, run_id)
            with Session(database.engine) as session:
                run = session.get(AgentRun, run_id)
                assert run is not None and run.status == "completed", (
                    run.failure_code if run else None
                )
                answer = session.get(Message, run.assistant_message_id)
                assert answer is not None and answer.content_text
                return answer.content_text

        if translation_scope == "full":
            full = translate("把 translation-live.md 全文翻译成英文")
            assert all(value in full for value in ("17", "480", "42", "900"))
            _assert_yuan_currency(full, 480)
            _assert_yuan_currency(full, 900)
            assert "星桥测试公司" in full
            assert re.search(r"per\s+night|each\s+night|a\s+night|nightly", full, re.I)
            assert not re.search(r"per[- ]diem|daily|per\s+day", full, re.I)
            assert all(str(number) in full for number in (*range(1101, 1131), *range(2201, 2231)))
            assert "第 2 段" in full and "[1]" in full and "days" in full.lower()
            return
        chapter = translate("把 translation-live.md 第二章翻译成英文并总结")
        assert "42" in chapter and "900" in chapter
        _assert_yuan_currency(chapter, 900)
        chapter_translation = chapter.split("### 译后总结", 1)[0]
        _assert_yuan_currency(chapter_translation, 900)
        assert "星桥测试公司" in chapter_translation
        assert re.search(
            r"(?:per|each|every)\s+(?:(?:piece|unit|item)\s+of\s+)?(?:equipment|device)"
            r"|(?:equipment|device)\s+(?:item|unit)|(?:per|each)\s+unit",
            chapter_translation,
            re.I,
        )
        assert all(str(number) in chapter for number in range(2201, 2231))
        assert re.search(r"(?<!\d)17(?!\d)", chapter) is None
        assert "480" not in chapter and "1101" not in chapter
        assert "译后总结" in chapter and "[1]" in chapter
        summary = chapter.split("### 译后总结", 1)[1]
        assert "42" in summary and "900" in summary
        _assert_yuan_currency(summary, 900)
        assert "星桥测试公司" in summary
