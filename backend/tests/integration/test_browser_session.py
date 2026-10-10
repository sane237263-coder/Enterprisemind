"""Opt-in disposable HTTP session for manual browser acceptance with selected providers."""

import asyncio
import io
import json
import os
import subprocess
import sys
import time

import httpx
import pymupdf
import pytest
from conftest import BACKEND, ROOT
from media_samples import docx_bytes, image_bytes, pptx_bytes, xlsx_bytes
from PIL import Image, ImageDraw, ImageFont

from enterprise_mind.agent.checkpoints import agent_event_loop, initialize_checkpoints

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EM_TEST_BROWSER_SESSION") != "1",
        reason="Interactive browser fixture requires EM_TEST_BROWSER_SESSION=1",
    ),
]


def test_interactive_browser_session(database, ingestion_settings):
    directory = ROOT / ".local" / "browser-session"
    directory.mkdir(parents=True, exist_ok=True)
    stop = directory / "stop"
    stop.unlink(missing_ok=True)
    services, _, _ = ingestion_settings
    assert database.url.database.startswith("em_test_")
    database.initialize()
    with asyncio.Runner(loop_factory=agent_event_loop) as runner:
        runner.run(initialize_checkpoints(database.url))
    env = os.environ.copy()
    for model in (database.settings, services):
        for field, value in model.model_dump().items():
            if value is not None:
                env["EM_" + field.upper()] = str(value)
    env["EM_PUBLIC_ORIGIN"] = "http://127.0.0.1:5173"
    env["PYTHONUTF8"] = "1"
    env.pop("EM_CHECKPOINT_POSTGRES_PASSWORD_FILE", None)
    samples = directory / "samples"
    samples.mkdir(exist_ok=True)
    (samples / "差旅制度.md").write_text(
        "# 星桥测试公司差旅制度\n\n差旅报销必须在出差结束后 17 天内提交。"
        "\n\n每晚住宿上限为 480 元。",
        encoding="utf-8",
    )
    for extension, builder in (
        ("docx", docx_bytes),
        ("pptx", pptx_bytes),
        ("xlsx", xlsx_bytes),
        ("png", image_bytes),
    ):
        (samples / f"合成资料.{extension}").write_bytes(builder())
    canvas = Image.new("RGB", (1100, 420), "white")
    ImageDraw.Draw(canvas).multiline_text(
        (40, 40),
        "SYNTHETIC TRAVEL POLICY\nSubmit expenses within 17 days.\nHotel limit: CNY 480 per night.",
        font=ImageFont.load_default(size=44),
        fill="black",
        spacing=26,
    )
    image_stream = io.BytesIO()
    canvas.save(image_stream, format="PNG")
    (samples / "文字图片.png").write_bytes(image_stream.getvalue())
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 90), "SYNTHETIC PDF POLICY", fontsize=20)
        page.insert_text((60, 135), "Submission deadline: 23 days after travel.", fontsize=16)
        pdf.save(samples / "PDF验收制度.pdf")
    processes = []
    try:
        commands = [
            [
                sys.executable,
                "-c",
                "import uvicorn; from enterprise_mind.app import create_app; "
                "from enterprise_mind.infrastructure.settings import "
                "APISettings, IngestionSettings; "
                "uvicorn.run(create_app(APISettings(), IngestionSettings()), host='127.0.0.1', "
                "port=18000, access_log=False, log_config=None, "
                "loop='enterprise_mind.agent.checkpoints:agent_event_loop')",
            ],
            [sys.executable, "-m", "enterprise_mind.cli", "dispatch"],
            [sys.executable, "-m", "enterprise_mind.cli", "worker"],
        ]
        for command in commands:
            processes.append(
                subprocess.Popen(
                    command,
                    env=env,
                    cwd=BACKEND,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            )
        with httpx.Client(base_url="http://127.0.0.1:18000", timeout=30, trust_env=False) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert all(p.poll() is None for p in processes), "Browser fixture process exited"
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
            else:
                pytest.fail("Browser fixture startup timed out")
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "初始管理员", "password": database.admin_password},
            )
            assert login.status_code == 200
            headers = {
                "Authorization": "Bearer " + login.json()["data"]["access_token"],
                "Idempotency-Key": "browser-fixture-admin",
            }
            # Public test-only credential, only in this disposable random database.
            result = client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={
                    "username": "验收管理员",
                    "password": "Browser-Check-Only-2026!",
                    "role": "admin",
                },
            )
            assert result.status_code == 201
        (directory / "session.json").write_text(
            json.dumps(
                {
                    "api_origin": "http://127.0.0.1:18000",
                    "browser_origin": "http://127.0.0.1:5173",
                    "username": "验收管理员",
                    "scope": "temporary_synthetic_browser_acceptance",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(
            "Disposable browser session ready; write .local/browser-session/stop to finish.",
            flush=True,
        )
        deadline = time.monotonic() + 7200
        while not stop.exists() and time.monotonic() < deadline:
            assert all(p.poll() is None for p in processes), "Browser fixture process exited"
            time.sleep(1)
        assert stop.exists(), "Browser acceptance session timed out"
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
