"""Long-lived synthetic browser acceptance session for Database MCP admin UI."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import BACKEND, ROOT
from phase8_helpers import (
    port_open,
    stop_process,
    wait_for_port,
    write_mcp_client_config,
)

from enterprise_mind.agent.checkpoints import agent_event_loop, initialize_checkpoints

pytest_plugins = ("phase8_helpers",)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EM_TEST_PHASE8_BROWSER") != "1",
        reason="Interactive browser fixture requires explicit EM_TEST_PHASE8_BROWSER=1",
    ),
]


def _environment(database, client_config: Path) -> dict[str, str]:
    env = os.environ.copy()
    for field, value in database.settings.model_dump().items():
        if value is not None:
            env["EM_" + field.upper()] = str(value)
    env["EM_DATABASE_CONFIG"] = str(client_config)
    env["EM_PUBLIC_ORIGIN"] = "http://127.0.0.1:5173"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(BACKEND / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("EM_CHECKPOINT_POSTGRES_PASSWORD_FILE", None)
    return env


def _wait_api(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 60
    with httpx.Client(base_url="http://127.0.0.1:18000", timeout=5, trust_env=False) as client:
        while time.monotonic() < deadline:
            assert process.poll() is None, "Browser API process exited during startup"
            try:
                if client.get("/health/ready").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
    raise AssertionError("Browser API did not become ready on 127.0.0.1:18000")


def _write_session_files(
    directory: Path,
    *,
    stop_file: Path,
    database,
    mcp,
    api_process: subprocess.Popen[bytes],
    worker_process: subprocess.Popen[bytes],
    vite_process: subprocess.Popen[bytes] | None,
    outsider_password: str,
) -> None:
    owned = {
        "api_pid": api_process.pid,
        "schema_refresh_worker_pid": worker_process.pid,
        "vite_pid": vite_process.pid if vite_process is not None else None,
    }
    session = {
        "api_origin": "http://127.0.0.1:18000",
        "browser_origin": "http://127.0.0.1:5173",
        "initial_source_configured": False,
        "accounts": [
            {
                "username": "初始管理员",
                "password": database.admin_password,
                "role": "admin",
            },
            {
                "username": "phase8-no-database-user",
                "password": outsider_password,
                "role": "user_without_relation_grant",
            },
        ],
        "source_form": {
            "name": mcp.business.source_body["name"],
            "host": "127.0.0.1",
            "port": mcp.business.port,
            "database_name": mcp.business.database_name,
            "ssl_mode": "disable",
            "secret_ref": mcp.business.source_metadata["secret_ref"],
            "connection_options": None,
            "mcp_endpoint": mcp.endpoint,
        },
        "synthetic_scope": {
            "paid_order_rows": 235,
            "amount_per_row": "10.00",
            "paid_order_total": "2350.00",
            "private_sentinel_is_not_a_source": True,
        },
        "stop_file": str(stop_file),
        "owned_processes": owned,
    }
    (directory / "session.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "OPERATIONS.md").write_text(
        "# Phase 8 browser session\n\n"
        "This is a disposable synthetic session. The database source starts unset.\n\n"
        f"1. Open `{session['browser_origin']}`.\n"
        "2. Log in with the admin account in `session.json`.\n"
        "3. Configure the exact non-secret source fields from `session.json`; the secret_ref "
        "is only a reference name and the actual MCP token remains in the fixture process.\n"
        "4. To stop, create the exact stop file (PowerShell):\n\n"
        f"   `New-Item -ItemType File -Force -LiteralPath '{stop_file}'`\n\n"
        "5. After the test exits, verify the listed PIDs and ports before stopping anything. "
        "Only stop the PIDs in `session.json`; do not stop all Python, Node, Docker or "
        "WSL processes.\n"
        "6. The fixture owns the random business database, readonly role, API process, "
        "schema-refresh worker and (when it started one) Vite process. It drops only that "
        "database and role during teardown.\n",
        encoding="utf-8",
    )


def test_phase8_interactive_browser_session(database, phase8_mcp, tmp_path):
    directory = ROOT / ".local" / "phase8-browser"
    directory.mkdir(parents=True, exist_ok=True)
    stop_file = directory / "stop"
    stop_file.unlink(missing_ok=True)
    database.initialize()
    client_config = write_mcp_client_config(
        phase8_mcp.business,
        phase8_mcp.endpoint,
        tmp_path / "database.yaml",
    )
    env = _environment(database, client_config)
    if env.get("EM_PROVIDERS_CONFIG") and env.get("EM_LLM_API_KEY_FILE"):
        with asyncio.Runner(loop_factory=agent_event_loop) as runner:
            runner.run(initialize_checkpoints(database.url))
    processes: list[subprocess.Popen[bytes]] = []
    vite_process: subprocess.Popen[bytes] | None = None
    if port_open(18000):
        pytest.fail("127.0.0.1:18000 is already in use; refusing to attach to an unknown process")
    try:
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import uvicorn; uvicorn.run('enterprise_mind.app:create_app', "
                "factory=True, host='127.0.0.1', port=18000, access_log=False, "
                "log_config=None, loop='enterprise_mind.agent.checkpoints:agent_event_loop')",
            ],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        processes.append(api_process)
        _wait_api(api_process)

        with httpx.Client(base_url="http://127.0.0.1:18000", timeout=30, trust_env=False) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "初始管理员", "password": database.admin_password},
            )
            assert login.status_code == 200, login.text
            admin = {
                "Authorization": "Bearer " + login.json()["data"]["access_token"],
                "Idempotency-Key": "phase8-browser-bootstrap",
            }
            department = client.post(
                "/api/v1/admin/departments",
                headers=admin,
                json={"name": "Phase 8 No Database Access"},
            )
            assert department.status_code == 201, department.text
            outsider_password = secrets.token_urlsafe(24)
            outsider = client.post(
                "/api/v1/admin/users",
                headers={**admin, "Idempotency-Key": "phase8-browser-no-database-user"},
                json={
                    "username": "phase8-no-database-user",
                    "password": outsider_password,
                    "role": "user",
                    "department_id": department.json()["data"]["id"],
                },
            )
            assert outsider.status_code == 201, outsider.text

        worker_process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "phase8_schema_refresh_worker.py"),
                "--stop-file",
                str(stop_file),
            ],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        processes.append(worker_process)
        time.sleep(0.5)
        assert worker_process.poll() is None, "Schema refresh worker exited during startup"

        if not port_open(5173):
            vite_env = os.environ.copy()
            vite_process = subprocess.Popen(
                ["npm.cmd", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"],
                cwd=ROOT / "frontend",
                env=vite_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            wait_for_port(vite_process, 5173)

        _write_session_files(
            directory,
            stop_file=stop_file,
            database=database,
            mcp=phase8_mcp,
            api_process=api_process,
            worker_process=worker_process,
            vite_process=vite_process,
            outsider_password=outsider_password,
        )
        print(
            "Phase-8 browser session ready; create .local/phase8-browser/stop to finish.",
            flush=True,
        )
        deadline = time.monotonic() + 7200
        while not stop_file.exists() and time.monotonic() < deadline:
            assert api_process.poll() is None, "Browser API process exited"
            assert worker_process.poll() is None, "Schema refresh worker exited"
            if vite_process is not None:
                assert vite_process.poll() is None, "Vite process exited"
            time.sleep(1)
        assert stop_file.exists(), "Browser acceptance session timed out"
    finally:
        stop_process(vite_process)
        for process in reversed(processes):
            stop_process(process)
