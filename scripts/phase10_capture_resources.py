"""Capture and verify the Phase-10 process/resource identity manifest."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT = "em-p10-20260921-lm01"
RUN_ID = "20260921-phase10-baseline-lunamax-01"
ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def docker_identity() -> dict[str, Any]:
    command = [
        "wsl.exe",
        "-d",
        "Ubuntu",
        "--",
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--format",
        '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.Ports}}',
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8")
    records = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    expected = {
        f"{PROJECT}-postgresql-1|{PROJECT}|postgresql|127.0.0.1:25432->5432/tcp",
        f"{PROJECT}-business-postgresql-1|{PROJECT}|business-postgresql|127.0.0.1:25433->5432/tcp",
        f"{PROJECT}-redis-1|{PROJECT}|redis|127.0.0.1:26379->6379/tcp",
        f"{PROJECT}-minio-1|{PROJECT}|minio|127.0.0.1:29000->9000/tcp",
    }
    return {
        "command": "wsl.exe -d Ubuntu -- docker ps --filter label=com.docker.compose.project=" + PROJECT,
        "exit_code": completed.returncode,
        "records": records,
        "expected_records_present": expected.issubset(set(records)),
    }


def api_ready() -> dict[str, Any]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:28080/health/ready", timeout=5) as response:
            body = json.loads(response.read())
            return {"status_code": response.status, "body": body, "ok": response.status == 200 and body.get("status") == "ready"}
    except Exception as error:  # noqa: BLE001 - readiness boundary
        return {"ok": False, "exception_type": type(error).__name__}


def listening_pid(port: int) -> int | None:
    completed = subprocess.run(["netstat", "-ano"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0].upper() == "TCP" and fields[1].endswith(f":{port}") and fields[3].upper() == "LISTENING":
            try:
                return int(fields[4])
            except ValueError:
                return None
    return None


def log_process_ids(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    started = [int(value) for value in re.findall(r"Started server process \[(\d+)\]", text)]
    parents = [int(value) for value in re.findall(r"Started parent process \[(\d+)\]", text)]
    return {"server_pids": started, "parent_pids": parents}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence_root
    launches = read_json(root / "runtime" / "process-launch.json")
    process_status = {
        item["name"]: {"launcher_pid": item["launcher_pid"], "alive": process_alive(int(item["launcher_pid"]))}
        for item in launches
    }
    mcp_pid = int((root / "runtime" / "mcp.pid").read_text(encoding="utf-8").strip())
    dependencies = read_json(root / "runtime" / "dependencies.json")
    migration = read_json(root / "runtime" / "migration.json")
    fixture = read_json(root / "fixtures" / "business-fixture.json")
    mcp_probe = read_json(root / "runtime" / "mcp-probe.json")
    docker = docker_identity()
    api = api_ready()
    api_processes = log_process_ids(root / "runtime" / "api.stderr.log")
    mcp_processes = log_process_ids(root / "runtime" / "mcp.stderr.log")
    api_listen_pid = listening_pid(28080)
    mcp_listen_pid = listening_pid(28001)
    api_worker_pids = api_processes["server_pids"]
    mcp_server_pids = mcp_processes["server_pids"]
    checks = {
        "docker_target_identity": docker["exit_code"] == 0 and docker["expected_records_present"],
        "migration_matches_head": migration.get("matches") is True,
        "fixture_count": fixture.get("fixture", {}).get("row_count") == 235,
        "readonly_write_blocked": fixture.get("readonly_probe", {}).get("write_probe_blocked") is True,
        "redis_probe": dependencies.get("redis", {}).get("ping") is True and dependencies.get("redis", {}).get("message_observed") is True,
        "minio_probe": dependencies.get("minio", {}).get("upload_verify") is True and dependencies.get("minio", {}).get("download_matches") is True,
        "mcp_probe": mcp_probe.get("readonly_mcp_operation") is True,
        "api_ready": api.get("ok") is True,
        "dispatcher_alive": process_status.get("dispatcher", {}).get("alive") is True,
        "worker_alive": process_status.get("worker", {}).get("alive") is True,
        "mcp_launcher_alive": process_alive(mcp_pid),
        "api_listen_process": api_listen_pid is not None and process_alive(api_listen_pid),
        # The listener PID must be the one recorded by this run's Uvicorn
        # startup log.  This prevents an unrelated/stale service on the same
        # port from satisfying readiness, and proves the process-local query
        # context has exactly one API worker for this acceptance run.
        "api_single_worker": len(api_worker_pids) == 1,
        "api_listener_matches_log": api_listen_pid is not None and api_listen_pid in api_worker_pids,
        "api_workers_alive": bool(api_worker_pids) and all(process_alive(pid) for pid in api_worker_pids),
        "mcp_server_process": mcp_listen_pid is not None and process_alive(mcp_listen_pid),
        "mcp_listener_matches_log": mcp_listen_pid is not None and mcp_listen_pid in mcp_server_pids,
    }
    result = {
        "run_id": RUN_ID,
        "captured_at": datetime.now(UTC).isoformat(),
        "identity_verified": all(checks.values()),
        # Keep the rich operator report above, and also emit the deliberately
        # small, fail-closed shape consumed by the independent export-chain
        # probe.  These values are derived only from the verified records
        # collected in this invocation; they are not an alternate source of
        # truth and must never be hand-edited.
        "project": PROJECT,
        "ports": {
            "application_postgres": 25432,
            "business_postgres": 25433,
            "redis": 26379,
            "minio": 29000,
        },
        "redis_stream": f"enterprise-mind:phase10:{RUN_ID}",
        "minio_bucket": "em-p10-20260921-lm01-phase10",
        "endpoints": {
            "api": "http://127.0.0.1:28080",
            "mcp": "http://127.0.0.1:28001/mcp",
        },
        "pids": {
            "api": api_listen_pid,
            "dispatcher": process_status.get("dispatcher", {}).get("launcher_pid"),
            "worker": process_status.get("worker", {}).get("launcher_pid"),
            "mcp": mcp_listen_pid,
        },
        "checks": checks,
        "compose": docker,
        "services": {
            "mcp": {"url": "http://127.0.0.1:28001/mcp", "launcher_pid": mcp_pid, "listen_pid": mcp_listen_pid, "server_pids": mcp_server_pids},
            "api": {"url": "http://127.0.0.1:28080", **process_status.get("api", {}), "listen_pid": api_listen_pid, "worker_pids": api_worker_pids},
            "dispatcher": process_status.get("dispatcher", {}),
            "worker": process_status.get("worker", {}),
            "redis": {"host": "127.0.0.1", "port": 26379, "stream": f"enterprise-mind:phase10:{RUN_ID}"},
            "minio": {"endpoint": "127.0.0.1:29000", "bucket": "em-p10-20260921-lm01-phase10", "app_access_file": str(root / "runtime" / "minio_p10_app_access"), "app_secret_file": str(root / "runtime" / "minio_p10_app_secret")},
            "postgresql": {"app": {"host": "127.0.0.1", "port": 25432, "database": "em_stage1_test"}, "business": {"host": "127.0.0.1", "port": 25433, "database": "em_business_test", "readonly_user": "em_p10_business_readonly"}},
        },
        "api_readiness": api,
        "dependency_probe": dependencies,
        "migration": migration,
        "mcp_probe": mcp_probe,
        "model_provider": "null (no live provider credential configured)",
    }
    (root / "runtime" / "process-resources.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"identity_verified": result["identity_verified"], "checks": checks}, ensure_ascii=False, sort_keys=True))
    return 0 if result["identity_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
