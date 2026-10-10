# ruff: noqa: F811
import asyncio

import pytest
import yaml
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401
from test_rag_pipeline import TestEmbedding, indexed, retrieve, upload_index  # noqa: F401

from enterprise_mind.infrastructure.settings import ConfigurationError
from enterprise_mind.rag.config import EmbeddingConfig, RAGSettings
from enterprise_mind.rag.models import Chunk, ChunkEmbedding
from enterprise_mind.rag.providers import ProviderFailure
from enterprise_mind.rag.rebuild import activate_embeddings, change_profiles, rebuild_embeddings
from enterprise_mind.rag.repository import RetrievalRepository
from enterprise_mind.rag.retrieval import RetrievalService

pytestmark = pytest.mark.integration


def target_model():
    return EmbeddingConfig(
        provider_key="test",
        base_url="https://model.test/v1",
        model="test-embedding-v2",
        model_key="test-v2",
        dimensions=3,
        max_input_tokens=8192,
        max_batch_size=1,
        timeout_seconds=5,
        max_retries=0,
        tokenizer_file="test-tokenizer.json",
        tokenizer_sha256="0" * 64,
    )


def profiles(tmp_path):
    current = tmp_path / "providers.yaml"
    target = tmp_path / "target.yaml"
    rag = tmp_path / "rag.yaml"
    model = target_model().model_dump(mode="json")
    target.write_text(
        yaml.safe_dump({"allowed_origins": ["https://model.test"], "embedding": model})
    )
    current.write_text(
        yaml.safe_dump(
            {
                "allowed_origins": ["https://model.test"],
                "embedding": {**model, "model_key": "test-v1", "model": "old-model"},
            }
        )
    )
    rag.write_text("rag: {}")
    return RAGSettings(providers_config=current, rag_config=rag), target


class Replacement(TestEmbedding):
    model_key = "test-v2"
    calls = 0
    fail_at = None

    async def embed(self, values):
        self.calls += 1
        if self.calls == self.fail_at:
            raise ProviderFailure("TEST_INTERRUPTED")
        return await super().embed(values)


def test_failed_backfill_resumes_and_keeps_old_retrieval(indexed, database):
    runtime = indexed[0]
    upload_index(indexed)
    upload_index(indexed, name="second.txt", body="ABC-123 第二份有效资料。")
    provider = Replacement()
    provider.fail_at = 2
    with pytest.raises(ProviderFailure, match="TEST_INTERRUPTED"):
        runtime.run(rebuild_embeddings, database.url, target_model(), provider)
    assert retrieve(indexed).contexts
    with Session(database.engine) as session:
        total = session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.status == "ready")
        )
        assert total >= 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(ChunkEmbedding)
                .where(ChunkEmbedding.model_key == "test-v1")
            )
            == total
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ChunkEmbedding)
                .where(ChunkEmbedding.model_key == "test-v2")
            )
            == 1
        )
    provider.fail_at = None
    report = runtime.run(rebuild_embeddings, database.url, target_model(), provider)
    assert report == {"written": total - 1, "remaining": 0}
    assert runtime.run(rebuild_embeddings, database.url, target_model(), provider)["written"] == 0
    assert retrieve(indexed).contexts
    config = indexed[1].config
    repository = RetrievalRepository(runtime.worker.sessions, config, "test-v2", 3)
    service = RetrievalService(repository, provider, indexed[4], config)
    assert retrieve((runtime, service, indexed[2])).contexts


def test_switch_rejects_running_services_then_missing_vectors_and_switches(
    indexed, database, tmp_path
):
    runtime = indexed[0]
    upload_index(indexed)
    settings, target = profiles(tmp_path)
    before = settings.providers_config.read_bytes()
    with pytest.raises(ConfigurationError, match="停止"):
        activate_embeddings(database.url, settings, target)
    assert settings.providers_config.read_bytes() == before
    # Dispose the fixture API pool so the cutover gate sees no live application sessions.
    runtime.run(runtime.client.app.state.agent.close)
    runtime.run(runtime.worker.sessions.kw["bind"].dispose)
    with pytest.raises(ConfigurationError, match="覆盖不完整"):
        activate_embeddings(database.url, settings, target)
    assert settings.providers_config.read_bytes() == before
    asyncio.run(rebuild_embeddings(database.url, target_model(), Replacement()))
    backup = activate_embeddings(database.url, settings, target)
    assert backup.read_bytes() == before
    output = yaml.safe_load(settings.providers_config.read_text())
    assert output["embedding"]["model_key"] == "test-v2"
    assert output["embedding"]["tokenizer_file"] == str(
        (tmp_path / "test-tokenizer.json").resolve()
    )
    with Session(database.engine) as session:
        assert set(session.scalars(select(ChunkEmbedding.model_key))) == {"test-v1", "test-v2"}


def test_rebuild_input_and_dimension_errors_leave_current_vectors(indexed, database):
    runtime = indexed[0]
    upload_index(indexed)
    provider = Replacement()
    with pytest.raises(ConfigurationError, match="输入上限"):
        runtime.run(
            rebuild_embeddings,
            database.url,
            target_model().model_copy(update={"max_input_tokens": 2}),
            provider,
        )
    assert provider.calls == 0
    provider.dimensions = 4
    with pytest.raises(ConfigurationError, match="不一致"):
        runtime.run(rebuild_embeddings, database.url, target_model(), provider)
    assert retrieve(indexed).contexts


def test_profile_guard_rejects_other_model_changes(tmp_path):
    settings, target = profiles(tmp_path)
    value = yaml.safe_load(target.read_text())
    value["embedding"]["model_key"] = "test-v1"
    target.write_text(yaml.safe_dump(value))
    with pytest.raises(ConfigurationError, match="不同"):
        change_profiles(settings, target)
    value["embedding"]["model_key"] = "test-v2"
    value["vision"] = {
        "provider_key": "test",
        "base_url": "https://model.test",
        "model": "vision",
        "model_key": "vision-v1",
    }
    target.write_text(yaml.safe_dump(value))
    with pytest.raises(ConfigurationError, match="其他模型"):
        change_profiles(settings, target)


def test_empty_corpus_cutover_does_not_fabricate_vectors(database, tmp_path):
    database.initialize()
    settings, target = profiles(tmp_path)
    # With an empty corpus no fabricated model vectors are required for a configuration cutover.
    before = settings.providers_config.read_bytes()
    backup = activate_embeddings(database.url, settings, target)
    assert backup.read_bytes() == before
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM knowledge.chunk_embeddings")) == 0


def test_cutover_rejects_unfinished_ingestion(indexed, database, tmp_path):
    runtime = indexed[0]
    runtime.upload()
    runtime.run(runtime.client.app.state.agent.close)
    runtime.run(runtime.worker.sessions.kw["bind"].dispose)
    settings, target = profiles(tmp_path)
    before = settings.providers_config.read_bytes()
    with pytest.raises(ConfigurationError, match="处理任务未完成"):
        activate_embeddings(database.url, settings, target)
    assert settings.providers_config.read_bytes() == before


def test_removed_chunk_during_model_call_is_not_recreated(indexed, database):
    runtime = indexed[0]
    upload_index(indexed)

    class RemovingProvider(Replacement):
        async def embed(self, values):
            with Session(database.engine) as session, session.begin():
                session.execute(delete(Chunk))
            return await super().embed(values)

    assert runtime.run(rebuild_embeddings, database.url, target_model(), RemovingProvider()) == {
        "written": 0,
        "remaining": 0,
    }
    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(ChunkEmbedding)) == 0


def test_failed_atomic_replace_keeps_current_profile_and_allows_retry(
    database, tmp_path, monkeypatch
):
    import enterprise_mind.rag.rebuild as module

    database.initialize()
    settings, target = profiles(tmp_path)
    before = settings.providers_config.read_bytes()
    replace = module.os.replace

    def fail_replace(*args):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        activate_embeddings(database.url, settings, target)
    assert settings.providers_config.read_bytes() == before
    assert not list(tmp_path.glob(".embedding-*"))
    monkeypatch.setattr(module.os, "replace", replace)
    backup = activate_embeddings(database.url, settings, target)
    assert backup.read_bytes() == before
