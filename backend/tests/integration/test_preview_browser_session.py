"""Opt-in browser verification on disposable data; no real model calls."""

# ruff: noqa: F811
import os
import subprocess
import sys
import time

import httpx
import pytest
from conftest import BACKEND, ROOT
from media_samples import docx_bytes, visual_pptx_bytes
from sqlalchemy import delete
from sqlalchemy.orm import Session
from test_ingestion import accepted, ingestion_runtime  # noqa: F401
from test_media_pipeline import media_indexed  # noqa: F401
from test_preview_rebuild import indexed_snapshot
from test_rag_pipeline import indexed  # noqa: F401
from test_slide_visuals import RenderFixture

from enterprise_mind.ingestion.models import DocumentArtifact
from enterprise_mind.ingestion.uploads import MIMES

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EM_TEST_PREVIEW_BROWSER") != "1",
        reason="Interactive preview browser fixture is opt-in",
    ),
]


def test_preview_browser_session(media_indexed, database, ingestion_settings, monkeypatch):
    import enterprise_mind.ingestion.preview_rebuild as service

    runtime = media_indexed[0][0]
    directory = ROOT / ".local/stage7-preview-browser"
    directory.mkdir(parents=True, exist_ok=True)
    stop, failure = directory / "stop", directory / "fail-next"
    stop.unlink(missing_ok=True)
    failure.unlink(missing_ok=True)
    original = service.prepare_preview

    async def controlled_failure(worker, lease):
        if failure.exists():
            failure.unlink()
            raise ValueError("synthetic preview failure")
        return await original(worker, lease)

    monkeypatch.setattr(service, "prepare_preview", controlled_failure)
    runtime.worker.media.converter = RenderFixture()
    identifiers = []
    for name, extension, body in (
        ("历史预览样本.docx", "docx", docx_bytes()),
        ("流程图.pptx", "pptx", visual_pptx_bytes()),
    ):
        item = accepted(runtime.upload([("files", (name, body, MIMES[extension]))]))
        runtime.run(runtime.dispatcher.dispatch_once)
        assert runtime.run(runtime.worker.run_once)
        identifiers.append(item["document_id"])
    snapshots = [indexed_snapshot(database, identifier) for identifier in identifiers]
    # Simulate a successful historical revision with no registered preview.
    with Session(database.engine) as session, session.begin():
        session.execute(
            delete(DocumentArtifact).where(
                DocumentArtifact.revision_id == snapshots[0][0],
                DocumentArtifact.artifact_type == "preview_html",
            )
        )
    created = runtime.client.post(
        "/api/v1/admin/users",
        headers={**runtime.admin, "Idempotency-Key": "browser-user"},
        json={"username": "预览验收员", "password": "Browser-Preview-Only-2026!", "role": "admin"},
    )
    assert created.status_code == 201
    env = os.environ.copy()
    for model in (database.settings, ingestion_settings[0]):
        for field, value in model.model_dump().items():
            if value is not None:
                env["EM_" + field.upper()] = str(value)
    env["EM_PROVIDERS_CONFIG"] = str(ROOT / "config/providers.yaml")
    env["EM_RAG_CONFIG"] = str(ROOT / "config/rag.yaml")
    env["EM_PUBLIC_ORIGIN"] = "http://127.0.0.1:5173"
    env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import uvicorn; from enterprise_mind.app import create_app; "
            "from enterprise_mind.infrastructure.settings import APISettings, IngestionSettings; "
            "uvicorn.run(create_app(APISettings(), IngestionSettings()), host='127.0.0.1', "
            "port=18000, access_log=False, log_config=None, "
            "loop='enterprise_mind.agent.checkpoints:agent_event_loop')",
        ],
        env=env,
        cwd=BACKEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        with httpx.Client(base_url="http://127.0.0.1:18000", trust_env=False, timeout=5) as client:
            for _ in range(100):
                assert process.poll() is None
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("browser API did not start")
        print(
            "Preview browser fixture ready; stop via .local/stage7-preview-browser/stop", flush=True
        )
        deadline = time.monotonic() + 2400
        while not stop.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            runtime.run(runtime.dispatcher.dispatch_once)
            runtime.run(runtime.worker.run_once)
        assert stop.exists(), "browser fixture timed out"
        assert [indexed_snapshot(database, identifier) for identifier in identifiers] == snapshots
    finally:
        process.terminate()
        process.wait(timeout=15)
