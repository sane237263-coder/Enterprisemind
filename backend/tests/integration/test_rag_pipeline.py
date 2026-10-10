from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_ingestion import (  # noqa: F401
    accepted,
    ingestion_runtime,
    worker_with_windows_parser_loop,
)

from enterprise_mind.api.errors import APIError
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import (
    Document,
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    Revision,
)
from enterprise_mind.ingestion.worker import IngestionWorker
from enterprise_mind.knowledge.models import KnowledgeBase
from enterprise_mind.rag.citations import citation_candidates, resolve_citation
from enterprise_mind.rag.config import RAGConfig
from enterprise_mind.rag.indexing import IndexingService
from enterprise_mind.rag.models import Chunk, ChunkAnchor, ChunkEmbedding
from enterprise_mind.rag.providers import ProviderFailure
from enterprise_mind.rag.repository import RetrievalRepository, Scope
from enterprise_mind.rag.retrieval import RetrievalService

pytestmark = pytest.mark.integration


class TestCounter:
    def count(self, value):
        return len(value)


class TestEmbedding:
    model_key = "test-v1"
    dimensions = 3
    counter = TestCounter()
    fail = False
    fail_term = None

    async def embed(self, values):
        if self.fail or (self.fail_term and any(self.fail_term in value for value in values)):
            raise ProviderFailure("TEST_FAILURE")
        return [[1.0, 1.0 if "ABC-123" in value else 0.2, 0.3] for value in values]


class TestRerank:
    model_key = "test-rerank-v1"
    counter = TestCounter()
    max_input_tokens = 4096
    score = 0.9
    fail = False

    async def rerank(self, question, documents):
        if self.fail:
            raise ProviderFailure("TEST_FAILURE")
        return [self.score for _ in documents]


@pytest.fixture
def indexed(runtime, database):
    config = RAGConfig(rerank={"model_key": "test-rerank-v1", "min_score": 0.7})
    embedding, reranker = TestEmbedding(), TestRerank()
    sessions = runtime.worker.sessions
    runtime.worker = worker_with_windows_parser_loop(
        IngestionWorker(
            sessions,
            runtime.worker.storage,
            runtime.worker.queue,
            runtime.worker.config,
            IndexingService(sessions, config, embedding),
        )
    )
    repository = RetrievalRepository(sessions, config, embedding.model_key, embedding.dimensions)
    service = RetrievalService(repository, embedding, reranker, config)
    with Session(database.engine) as session:
        user = session.get(User, database.user_id())
        identity = CurrentIdentity(user, None, user.credential_version)
    return runtime, service, identity, embedding, reranker


def upload_index(
    indexed, name="manual.txt", body="ABC-123 支持企业加密。\n\n管理员需要使用密钥文件。"
):
    runtime, *_ = indexed
    item = accepted(runtime.upload([("files", (name, body.encode(), "text/plain"))]))
    runtime.run(runtime.dispatcher.dispatch_once)
    assert runtime.run(runtime.worker.run_once)
    return item


def retrieve(indexed, question="ABC-123", **kwargs):
    runtime, service, identity, *_ = indexed

    async def run():
        return await service.retrieve(
            identity, question, available_input_tokens=20000, counter=TestCounter(), **kwargs
        )

    return runtime.run(run)


def test_upload_to_index_retrieval_citation_and_duplicate_event(indexed, database):
    item = upload_index(indexed)
    runtime, service, identity, *_ = indexed
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document.processing_status == "success"
        assert document.active_revision_id is not None
        chunks = session.scalars(select(Chunk)).all()
        assert chunks and all(chunk.status == "ready" for chunk in chunks)
        assert session.scalar(select(func.count()).select_from(ChunkEmbedding)) == len(chunks)
        count = len(chunks)
        event = session.scalars(select(OutboxEvent)).one()
        runtime.run(runtime.worker.handle_event, event.id)
        assert session.scalar(select(func.count()).select_from(Chunk)) == count
    result = retrieve(indexed)
    assert result.status == "ok" and result.contexts
    choices = citation_candidates(result)
    chosen = next(iter(choices.values()))
    citation = runtime.run(
        resolve_citation, service.repository, identity, choices, chosen.key, "ABC-123"
    )
    assert citation.document_id == int(item["document_id"])
    assert citation.quote_text == "ABC-123" and citation.locator["char_start"] == 0
    with pytest.raises(APIError):
        runtime.run(
            resolve_citation, service.repository, identity, choices, chosen.key, "模型编造片段"
        )


def test_embedding_failure_never_activates_or_keyword_recalls(indexed, database):
    indexed[3].fail = True
    upload_index(indexed)
    with Session(database.engine) as session:
        document = session.scalars(select(Document)).one()
        assert document.active_revision_id is None and document.processing_status == "failed"
        assert set(session.scalars(select(Chunk.status))) == {"failed"}
        assert session.scalars(select(ProcessingTask)).one().finished_at
    indexed[3].fail = False
    assert retrieve(indexed).status == "insufficient_evidence"


def test_rerank_threshold_fallback_budget_and_model_mismatch(indexed):
    upload_index(indexed)
    indexed[4].score = 0.1
    assert retrieve(indexed).status == "insufficient_evidence"
    indexed[4].fail = True
    result = retrieve(indexed)
    assert result.status == "ok" and result.warnings
    indexed[3].fail = True
    result = retrieve(indexed, "不存在的信息")
    assert result.status == "insufficient_evidence" and len(result.warnings) >= 2
    result = retrieve(indexed, "ABC-123")
    assert result.status == "ok" and result.warnings
    indexed[1].config.context.absolute_max_tokens = 2
    assert retrieve(indexed, "ABC-123").status == "insufficient_evidence"


@pytest.mark.parametrize("by_name", [False, True])
def test_department_isolation_revocation_and_scope_before_limit(indexed, database, by_name):
    item = upload_index(indexed)
    runtime, service, identity, *_ = indexed
    with Session(database.engine, expire_on_commit=False) as session, session.begin():
        department = Department(name="研发", name_key="研发")
        other = Department(name="销售", name_key="销售")
        session.add_all([department, other])
        session.flush()
        user = User(
            username="普通用户",
            username_key="普通用户",
            role="user",
            department_id=department.id,
            password_hash=identity.user.password_hash,
        )
        session.add(user)
        session.flush()
        user_identity = CurrentIdentity(user, department, user.credential_version)
        department_id, other_id = department.id, other.id

    async def fetch():
        return await service.retrieve(
            user_identity,
            "ABC-123",
            available_input_tokens=20000,
            counter=TestCounter(),
            scope=Scope(document_names=("manual.txt",)) if by_name else Scope(),
        )

    assert runtime.run(fetch).status == "ok"
    with Session(database.engine) as session, session.begin():
        base = session.get(KnowledgeBase, int(runtime.base_id))
        base.type, base.department_id = "department", other_id
    assert runtime.run(fetch).status == "insufficient_evidence"
    with pytest.raises(APIError):
        runtime.run(
            service.repository.validate_scope,
            user_identity,
            Scope(document_ids=(int(item["document_id"]),)),
        )
    with Session(database.engine) as session, session.begin():
        session.get(KnowledgeBase, int(runtime.base_id)).department_id = department_id
    assert runtime.run(fetch).status == "ok"
    with Session(database.engine) as session, session.begin():
        session.get(User, user_identity.user.id).status = "disabled"
    with pytest.raises(APIError):
        runtime.run(fetch)


def test_provider_wait_rechecks_permission_before_return(indexed, database):
    upload_index(indexed)
    original = indexed[4].rerank

    async def revoke(question, documents):
        scores = await original(question, documents)
        with Session(database.engine) as session, session.begin():
            session.get(User, indexed[2].user.id).credential_version += 1
        return scores

    indexed[4].rerank = revoke
    with pytest.raises(APIError):
        retrieve(indexed)


def test_stale_index_lease_cannot_persist_and_recovery_reuses_work(indexed, database):
    runtime, _, _, *_ = indexed
    item = accepted(runtime.upload())
    task_id = int(item["processing_task_id"])
    parse = runtime.run(runtime.worker.claim, task_id)
    assert runtime.run(runtime.worker.execute, parse)
    chunk_lease = runtime.run(runtime.worker.claim, task_id)
    prepared = runtime.run(runtime.worker.indexing.prepare, chunk_lease)
    with Session(database.engine) as session, session.begin():
        session.get(ProcessingUnit, chunk_lease.unit_id).lease_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    assert not runtime.run(runtime.worker.persist, chunk_lease, None, prepared.error, prepared)
    assert runtime.run(runtime.dispatcher.recover) == 1
    fresh = runtime.run(runtime.worker.claim, task_id)
    assert runtime.run(runtime.worker.execute, fresh)
    assert not runtime.run(runtime.worker.persist, chunk_lease, None, prepared.error, prepared)
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Chunk)) == 1


def test_database_rejects_cross_revision_anchor_and_modified_source(indexed, database):
    upload_index(indexed)
    upload_index(indexed, "other.txt", "不同来源。")
    with Session(database.engine) as session:
        chunks = session.scalars(select(Chunk).order_by(Chunk.id)).all()
        mappings = session.scalars(select(ChunkAnchor).order_by(ChunkAnchor.id)).all()
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(
            text("UPDATE knowledge.chunk_anchors SET anchor_id=:anchor WHERE id=:id"),
            {"anchor": mappings[-1].anchor_id, "id": mappings[0].id},
        )
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(
            text("UPDATE knowledge.chunks SET content='篡改' WHERE id=:id"), {"id": chunks[0].id}
        )


def test_explicit_empty_intersection_returns_no_context(indexed):
    item = upload_index(indexed)
    assert (
        retrieve(
            indexed,
            scope=Scope(document_ids=(int(item["document_id"]),)),
            agent_scope=Scope(document_ids=()),
        ).status
        == "insufficient_evidence"
    )


def test_neighbors_are_only_immediately_adjacent_in_the_same_structure(indexed, database):
    upload_index(indexed, body="ABC-123 同一长段落中的连续内容。" * 160)
    runtime, service, identity, *_ = indexed
    with Session(database.engine) as session:
        ids = list(session.scalars(select(Chunk.id).order_by(Chunk.ordinal)))
    assert len(ids) >= 4
    seed = runtime.run(service.repository.load, identity, Scope(), [ids[2]])[ids[2]]
    neighbors = runtime.run(service.repository.neighbors, identity, Scope(), seed)
    assert set(neighbors) == {ids[1], ids[3]}
    assert all(candidate.context_group == seed.context_group for candidate in neighbors.values())
    assert not runtime.run(service.repository.neighbors, identity, Scope(document_ids=()), seed)


def test_one_invalid_anchor_excludes_the_entire_chunk(indexed, database):
    upload_index(indexed)
    with Session(database.engine) as session:
        mappings = session.scalars(select(ChunkAnchor).order_by(ChunkAnchor.ordinal)).all()
        assert len(mappings) >= 2 and len({mapping.chunk_id for mapping in mappings}) == 1
        chunk_id, anchor_id = mappings[0].chunk_id, mappings[0].anchor_id
    # Simulate a damaged checksum without changing the protected indexed source text.
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE knowledge.source_anchors SET source_text_sha256=:digest WHERE id=:id"),
            {"digest": "0" * 64, "id": anchor_id},
        )
    runtime, service, identity, *_ = indexed
    assert runtime.run(service.repository.load, identity, Scope(), [chunk_id]) == {}
    assert retrieve(indexed).status == "insufficient_evidence"


def test_partial_success_excludes_failed_chunks_and_keeps_usable_content(indexed, database):
    indexed[3].fail_term = "FAIL_BLOCK"
    item = upload_index(
        indexed,
        body="ABC-123 可检索的有效内容。" * 8 + "\n\n" + "FAIL_BLOCK 此段向量服务失败。" * 8,
    )
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document.processing_status == "partial_success"
        revision = session.get(Revision, document.active_revision_id)
        assert revision.failure_summary["units"][0]["error_code"] == "EMBEDDING_FAILED"
        assert set(session.scalars(select(Chunk.status))) == {"ready", "failed"}
    result = retrieve(indexed)
    assert result.status == "ok"
    assert all("FAIL_BLOCK" not in block.content for block in result.contexts)


def test_new_pending_or_failed_revision_keeps_old_active_content(indexed, database):
    item = upload_index(indexed)
    with Session(database.engine) as session, session.begin():
        document = session.get(Document, int(item["document_id"]))
        active_id = document.active_revision_id
        document.latest_revision_no = 2
        document.processing_status = "processing"
    assert retrieve(indexed).status == "ok"
    with Session(database.engine) as session, session.begin():
        session.get(Document, int(item["document_id"])).processing_status = "failed"
    assert retrieve(indexed).contexts[0].candidates[0].revision_id == active_id


def test_metadata_is_live_and_specific_document_scope_does_not_expand(indexed, database):
    first = upload_index(indexed, "first.txt", "ABC-123 当前资料。")
    second = upload_index(indexed, "second.txt", "ABC-123 另一文档。")
    with Session(database.engine) as session, session.begin():
        document = session.get(Document, int(first["document_id"]))
        document.name, document.name_key = "唯一采购协议.txt", "唯一采购协议.txt"
    runtime, service, identity, *_ = indexed
    rows = runtime.run(service.repository.metadata, identity, Scope(), "唯一采购协议")
    loaded = runtime.run(service.repository.load, identity, Scope(), [row[0] for row in rows])
    assert loaded and {item.document_id for item in loaded.values()} == {int(first["document_id"])}
    result = retrieve(indexed, scope=Scope(document_ids=(int(second["document_id"]),)))
    assert {c.document_id for b in result.contexts for c in b.candidates} == {
        int(second["document_id"])
    }


@pytest.mark.parametrize(
    "question", ["数据分级", "data classification", "数据分级 data classification"]
)
def test_multilingual_recall_channels_keep_the_same_document_scope(indexed, database, question):
    chosen = upload_index(
        indexed,
        "数据分级 data classification.txt",
        "SEC-492 数据分级要求加密财务报表。"
        "Data classification requires encrypted financial reports.",
    )
    excluded = upload_index(
        indexed,
        "excluded data classification.txt",
        "SEC-492 数据分级属于另一份资料。Data classification uses a different policy.",
    )
    runtime, service, identity, embedding, _ = indexed
    scope = Scope(document_ids=(int(chosen["document_id"]),))
    vector = runtime.run(embedding.embed, [question])[0]
    for method, argument in (
        (service.repository.keyword, question),
        (service.repository.metadata, question),
        (service.repository.vector, vector),
    ):
        rows = runtime.run(method, identity, scope, argument)
        assert rows, (question, method.__name__)
        loaded = runtime.run(service.repository.load, identity, scope, [row[0] for row in rows])
        assert {item.document_id for item in loaded.values()} == {int(chosen["document_id"])}
        assert int(excluded["document_id"]) not in {item.document_id for item in loaded.values()}
    result = retrieve(indexed, question, scope=scope)
    assert result.status == "ok"
    assert {item.document_id for block in result.contexts for item in block.candidates} == {
        int(chosen["document_id"])
    }
    choices = citation_candidates(result)
    citation = next(iter(choices.values()))
    resolved = runtime.run(
        resolve_citation, service.repository, identity, choices, citation.key, citation.quote
    )
    assert resolved.document_id == int(chosen["document_id"])

    with Session(database.engine, expire_on_commit=False) as session, session.begin():
        allowed = Department(name="多语言检索组", name_key="多语言检索组")
        restricted = Department(name="受限资料组", name_key="受限资料组")
        session.add_all([allowed, restricted])
        session.flush()
        user = User(
            username="多语言检索用户",
            username_key="多语言检索用户",
            role="user",
            department_id=allowed.id,
            password_hash=identity.user.password_hash,
        )
        session.add(user)
        session.flush()
        user_identity = CurrentIdentity(user, allowed, user.credential_version)
        base = session.get(KnowledgeBase, int(runtime.base_id))
        base.type, base.department_id = "department", restricted.id

    for method, argument in (
        (service.repository.keyword, question),
        (service.repository.metadata, question),
        (service.repository.vector, vector),
    ):
        assert runtime.run(method, user_identity, scope, argument) == []

    async def denied_retrieval():
        return await service.retrieve(
            user_identity,
            question,
            scope=scope,
            available_input_tokens=20000,
            counter=TestCounter(),
        )

    with pytest.raises(APIError):
        runtime.run(denied_retrieval)


def test_synthetic_policy_corpus_preserves_version_and_source_scope(indexed):
    corpus = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_corpus"
    documents = {
        name: upload_index(indexed, name, (corpus / name).read_text(encoding="utf-8"))
        for name in (
            "travel_2025.md",
            "travel_2026.md",
            "procurement_2026.md",
            "incident_2026.md",
        )
    }
    cases = (
        ("travel_2025.md", "2025 年差旅报销期限", "17 天内", "21 天内"),
        ("travel_2026.md", "2026 travel reimbursement deadline", "21 天内", "17 天内"),
        ("procurement_2026.md", "5000 元采购审批边界", "达到或超过 5000 元", "480 元"),
        ("incident_2026.md", "客户数据泄露安全事件报告时限", "2 小时内", "21 天内"),
    )
    runtime, service, identity, *_ = indexed

    async def resolve_in_scope(choices, key, quote, document_id):
        return await resolve_citation(
            service.repository,
            identity,
            choices,
            key,
            quote,
            scope=Scope(document_ids=(document_id,)),
        )

    for filename, question, expected, excluded in cases:
        document_id = int(documents[filename]["document_id"])
        result = retrieve(indexed, question, scope=Scope(document_ids=(document_id,)))
        assert result.status == "ok", filename
        assert {item.document_id for block in result.contexts for item in block.candidates} == {
            document_id
        }
        content = "\n".join(block.content for block in result.contexts)
        assert expected in content and excluded not in content, filename
        choices = citation_candidates(result)
        chosen = next(item for item in choices.values() if expected in item.quote)
        citation = runtime.run(resolve_in_scope, choices, chosen.key, expected, document_id)
        assert citation.document_id == document_id

    travel_ids = tuple(
        int(documents[name]["document_id"]) for name in ("travel_2025.md", "travel_2026.md")
    )
    compared = retrieve(
        indexed,
        "比较 2025 和 2026 年差旅报销期限与住宿上限",
        scope=Scope(document_ids=travel_ids),
    )
    assert compared.status == "ok"
    assert {item.document_id for block in compared.contexts for item in block.candidates} == set(
        travel_ids
    )


def test_named_document_scope_never_falls_back_to_other_documents(indexed, database):
    first = upload_index(indexed, "first.txt", "ABC-123 当前资料。")
    second = upload_index(indexed, "second.txt", "ABC-123 另一文档。")
    runtime, service, identity, *_ = indexed
    names = Scope(document_names=(" SECOND.TXT ",))
    result = retrieve(indexed, scope=names)
    assert {c.document_id for b in result.contexts for c in b.candidates} == {
        int(second["document_id"])
    }
    assert (
        retrieve(indexed, scope=Scope(document_names=("absent.txt",))).status
        == "insufficient_evidence"
    )
    assert retrieve(indexed, scope=Scope(document_names=())).status == "insufficient_evidence"
    assert (
        retrieve(
            indexed, scope=names, agent_scope=Scope(document_ids=(int(first["document_id"]),))
        ).status
        == "insufficient_evidence"
    )
    assert (
        retrieve(indexed, scope=names, agent_scope=Scope(extensions=("pdf",))).status
        == "insufficient_evidence"
    )
    candidate_ids = [c.id for b in result.contexts for c in b.candidates]
    with Session(database.engine) as session, session.begin():
        doc = session.get(Document, int(second["document_id"]))
        doc.name = doc.name_key = "renamed.txt"
    # Citations are revalidated with the same name scope after metadata changes.
    assert runtime.run(service.repository.load, identity, names, candidate_ids) == {}


def test_hnsw_matches_exact_and_missing_index_falls_back(indexed, database):
    from enterprise_mind.rag.config import EmbeddingConfig
    from enterprise_mind.rag.vector_index import build_vector_index, index_name

    upload_index(indexed)
    runtime, service, identity, embedding, *_ = indexed
    with database.engine.begin() as connection:
        connection.execute(
            text("""
INSERT INTO knowledge.chunks (revision_id,document_id,knowledge_base_id,ordinal,chunk_type,
    content,search_tokens,search_vector,token_count,status)
SELECT revision_id,document_id,knowledge_base_id,n,'text',content,search_tokens,search_vector,
    token_count,'ready' FROM knowledge.chunks c CROSS JOIN generate_series(1,1500) n
WHERE c.ordinal=0;
INSERT INTO knowledge.chunk_anchors
    (chunk_id,anchor_id,ordinal,content_start,content_end,quote_text)
SELECT c.id,a.anchor_id,0,a.content_start,a.content_end,a.quote_text
FROM knowledge.chunks c CROSS JOIN (SELECT * FROM knowledge.chunk_anchors LIMIT 1) a
WHERE c.ordinal>0;
INSERT INTO knowledge.chunk_embeddings (chunk_id,model_key,dimensions,embedding,status)
SELECT id,'test-v1',3,('[1,' || (ordinal::float/1000) || ',0.3]')::vector,'ready'
FROM knowledge.chunks WHERE ordinal>0;
ANALYZE knowledge.chunks;
ANALYZE knowledge.chunk_embeddings;
""")
        )
    repository = service.repository
    repository.config.vector.candidate_limit = 10
    exact = runtime.run(repository.vector, identity, Scope(), [1.0, 0.71, 0.3])
    model = EmbeddingConfig(
        provider_key="test",
        base_url="https://test.invalid/v1",
        model="test",
        model_key=embedding.model_key,
        dimensions=3,
        timeout_seconds=60,
        max_input_tokens=600,
        tokenizer_file="unused-in-index-command",
        tokenizer_sha256="0" * 64,
    )
    build_vector_index(database.url, model, repository.config.hnsw)
    repository.config.vector.exact_scan_max_chunks = 2
    parameters_for_query = repository.parameters

    async def prefer_index(session, actor, scope):
        # Small fixtures favor a sequential scan. Exercise the real ANN path locally as well.
        params = await parameters_for_query(session, actor, scope)
        await session.execute(text("SET LOCAL enable_seqscan=off"))
        await session.execute(text("SET LOCAL enable_sort=off"))
        await session.execute(text("SET LOCAL enable_bitmapscan=off"))
        return params

    repository.parameters = prefer_index
    statements = []
    engine = runtime.worker.sessions.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    approximate = runtime.run(repository.vector, identity, Scope(), [1.0, 0.71, 0.3])
    event.remove(engine, "before_cursor_execute", capture)
    used_hnsw = any(
        "ORDER BY e.embedding::vector" in sql and "EXPLAIN" not in sql for sql, _ in statements
    )
    assert used_hnsw
    repository.parameters = parameters_for_query
    assert {row[0] for row in approximate} == {row[0] for row in exact}
    with database.engine.begin() as connection:
        connection.execute(text(f"DROP INDEX knowledge.{index_name(embedding.model_key, 3)}"))
    assert runtime.run(repository.vector, identity, Scope(), [1.0, 0.71, 0.3]) == exact


def test_recycle_bin_defers_activation_until_restored(indexed, database):
    from enterprise_mind.knowledge.models import DeletionBatch

    runtime = indexed[0]
    item = accepted(runtime.upload())
    task_id = int(item["processing_task_id"])
    lease = runtime.run(runtime.worker.claim, task_id)
    runtime.run(runtime.worker.execute, lease)
    with Session(database.engine) as session, session.begin():
        now = datetime.now(UTC)
        batch = DeletionBatch(
            root_type="document",
            root_id=int(item["document_id"]),
            root_name="测试文档",
            deleted_at=now,
            purge_after=now + timedelta(days=30),
            status="active",
        )
        session.add(batch)
        session.flush()
        document = session.get(Document, int(item["document_id"]))
        document.deleted_at, document.purge_after, document.deletion_batch_id = (
            now,
            batch.purge_after,
            batch.id,
        )
        event_id = session.scalars(select(OutboxEvent.id)).one()
    assert runtime.run(runtime.worker.handle_event, event_id) is False
    with Session(database.engine) as session, session.begin():
        document = session.get(Document, int(item["document_id"]))
        assert document.active_revision_id is None
        document.deleted_at = document.purge_after = document.deletion_batch_id = None
    assert runtime.run(runtime.worker.handle_event, event_id)
    assert retrieve(indexed).status == "ok"


def test_unfinished_ocr_source_does_not_skip_or_block_other_source_work(indexed, database):
    import pymupdf

    runtime = indexed[0]
    with pymupdf.open() as pdf:
        pdf.new_page()
        page = pdf.new_page()
        page.insert_text((72, 72), "ABC-123 Native text")
        payload = pdf.tobytes()
    item = accepted(runtime.upload([("files", ("scan-and-text.pdf", payload, "application/pdf"))]))
    runtime.run(runtime.dispatcher.dispatch_once)
    runtime.run(runtime.worker.run_once)
    with Session(database.engine) as session:
        document = session.get(Document, int(item["document_id"]))
        assert document.processing_status == "processing" and document.active_revision_id is None
        assert (
            session.scalar(select(func.count()).select_from(Chunk).where(Chunk.status == "ready"))
            == 1
        )
        assert set(
            session.scalars(select(ProcessingUnit.status).where(ProcessingUnit.stage == "ocr"))
        ) == {"pending"}
    assert retrieve(indexed).status == "insufficient_evidence"
