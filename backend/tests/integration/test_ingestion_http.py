import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from websockets.sync.client import connect

pytestmark = pytest.mark.integration


def test_real_http_ws_cli_and_restart_replay(database, ingestion_settings):
    database.initialize()
    services, _, _ = ingestion_settings
    env = os.environ.copy()
    for field, value in database.settings.model_dump().items():
        if value is not None:
            env["EM_" + field.upper()] = str(value)
    for field, value in services.model_dump().items():
        env["EM_" + field.upper()] = str(value)
    env["EM_ENV"] = "development"
    env["PYTHONUTF8"] = "1"
    env.pop("EM_BOOTSTRAP_ADMIN_USERNAME", None)
    env.pop("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", None)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def start():
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "enterprise_mind.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(process):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def ready(client, process):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                response = client.get("/health/ready")
                if response.status_code == 200:
                    assert response.json()["stage"] == "ingestion_foundation"
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        pytest.fail("API readiness timeout")

    process = start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
        ) as client:
            ready(client, process)
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "初始管理员", "password": database.admin_password},
            )
            assert login.status_code == 200
            token = login.json()["data"]["access_token"]
            headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "http-base"}
            base = client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "HTTP测试", "type": "public"},
            )
            assert base.status_code == 201
            path = f"/api/v1/knowledge-bases/{base.json()['data']['id']}/document-uploads"
            headers["Idempotency-Key"] = "http-upload"
            # Exercise SDK multipart streaming beyond its 5 MiB part boundary.
            content = b"line of document content\n" * 250000
            first = client.post(
                path, headers=headers, files=[("files", ("large.txt", content, "text/plain"))]
            )
            assert first.status_code == 202, first.text
            document_id = first.json()["data"]["items"][0]["document_id"]
            for command in ("dispatch", "worker"):
                result = subprocess.run(
                    [sys.executable, "-m", "enterprise_mind.cli", command, "--once"],
                    env=env,
                    capture_output=True,
                    timeout=60,
                )
                assert result.returncode == 0, "Processing CLI failed without exposing credentials"
            progress = client.get(f"/api/v1/documents/{document_id}/processing", headers=headers)
            assert progress.json()["data"]["current_stage"] == "chunk", progress.text
            with connect(f"ws://127.0.0.1:{port}/api/v1/ws/document-processing", proxy=None) as ws:
                import json

                ws.send(
                    json.dumps(
                        {"type": "subscribe", "access_token": token, "document_ids": [document_id]}
                    )
                )
                event = json.loads(ws.recv(timeout=5))
                assert event["data"]["current_stage"] == "chunk"
                assert "source_text" not in str(event)
            stop(process)
            process = start()
            ready(client, process)
            replay = client.post(
                path, headers=headers, files=[("files", ("large.txt", content, "text/plain"))]
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["data"] == first.json()["data"]
    finally:
        stop(process)
