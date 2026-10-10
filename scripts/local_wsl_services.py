"""Manage only EM's four native Ubuntu systemd services."""
import argparse
import grp
import json
import os
import pwd
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local/runtime/wsl"
SERVICES = ("api", "dispatcher", "worker", "frontend") + (("database-mcp",) if (WORK / "demo-source.json").is_file() else ())
MARKER = "# Managed by EnterpriseMind local_wsl_services.py\n"
NAMES = [f"enterprise-mind-local-{name}.service" for name in SERVICES]


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/systemctl", *arguments], check=check,
                          text=True, capture_output=True, timeout=90)


def quote(value: str) -> str:
    if any(ch in value for ch in "\x00\n\r%"):
        raise RuntimeError("unsupported systemd path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install() -> None:
    paths = json.loads((WORK / "paths.json").read_text())
    user = pwd.getpwnam(paths["user"])
    if user.pw_uid < 1000 or Path(paths["state"]).resolve() != Path(user.pw_dir) / ".local/share/enterprise-mind":
        raise RuntimeError("development user or managed tool directory mismatch")
    python = paths["python"]
    node = paths["node"]
    commands = {
        "api": [python, "-m", "enterprise_mind.cli", "serve", "--host", "127.0.0.1", "--port", "18000"],
        "dispatcher": [python, "-m", "enterprise_mind.cli", "dispatch"],
        "worker": [python, "-m", "enterprise_mind.cli", "worker"],
        "frontend": [node, str(Path(paths["frontend"]) / "node_modules/vite/bin/vite.js"),
                     "--host", "127.0.0.1", "--port", "5173"],
        "database-mcp": [python, "-m", "enterprise_mind.database.mcp_server"],
    }
    for name in SERVICES:
        path = Path("/etc/systemd/system") / f"enterprise-mind-local-{name}.service"
        if path.exists() and not path.read_text().startswith(MARKER):
            raise RuntimeError("existing systemd unit is not managed by EM")
        if systemctl("is-active", path.name, check=False).stdout.strip() == "active":
            raise RuntimeError("stop EM native services before updating units")
    for name in SERVICES:
        working = paths["frontend"] if name == "frontend" else str(ROOT)
        quote(working)
        quote(str(WORK / "runtime.env"))
        path = Path("/etc/systemd/system") / f"enterprise-mind-local-{name}.service"
        # Vite can handle a requested SIGTERM by returning 128 + 15 (143).
        # Accept that exit only for the frontend; backend failure rules stay intact.
        path.write_text(MARKER + f"""[Unit]
Description=EnterpriseMind local {name}
After=network.target postgresql.service docker.service
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User={user.pw_name}
Group={grp.getgrgid(user.pw_gid).gr_name}
WorkingDirectory={working}
EnvironmentFile={WORK / 'runtime.env'}
ExecStart={' '.join(quote(part) for part in commands[name])}
Restart=on-failure
RestartSec=3
{"SuccessExitStatus=143" if name == "frontend" else ""}
KillMode=control-group
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=append:{WORK}/{name}.stdout.log
StandardError=append:{WORK}/{name}.stderr.log

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    verified = subprocess.run(["/usr/bin/systemd-analyze", "verify", *[
        str(Path("/etc/systemd/system") / name) for name in NAMES
    ]], check=False, capture_output=True, text=True, timeout=30)
    if verified.returncode or verified.stderr.strip():
        raise RuntimeError("generated EM service units did not pass systemd verification")
    systemctl("daemon-reload")
    print(json.dumps({"units_installed": NAMES, "boot_autostart_enabled": False}))


def managed_units() -> list[str]:
    units = []
    for name in NAMES:
        path = Path("/etc/systemd/system") / name
        if not path.exists():
            continue
        if not path.read_text().startswith(MARKER):
            raise RuntimeError("refusing to operate on an unmanaged systemd unit")
        units.append(name)
    return units


def wait_service(name: str, port: int | None = None) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = systemctl("show", name, "--property=ActiveState", "--value").stdout.strip()
        if state == "failed":
            raise RuntimeError(f"EM service failed during startup: {name}")
        if state == "active":
            if port is None:
                time.sleep(2)
                return
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
                if port in (18000, 5173):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=2) as response:
                        if response.status != 200:
                            raise RuntimeError("not ready")
                return
            except (OSError, RuntimeError):
                pass
        time.sleep(1)
    raise RuntimeError(f"EM service did not become ready: {name}")


def start_services(units: list[str]) -> None:
    ports = {"database-mcp": 38001, "api": 18000, "frontend": 5173}
    # Warm shared imports one service at a time on this constrained WSL host.
    for service in ("database-mcp", "api", "dispatcher", "worker", "frontend"):
        name = f"enterprise-mind-local-{service}.service"
        if name not in units:
            continue
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available_kib = int(memory["MemAvailable"].split()[0])
        print(json.dumps({"starting_service": service, "ubuntu_available_mib": available_kib // 1024}), flush=True)
        systemctl("start", name)
        wait_service(name, ports.get(service))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "start", "stop", "status"])
    action = parser.parse_args().action
    if os.geteuid() != 0:
        raise RuntimeError("systemd control requires the Ubuntu root entry point")
    if action == "install":
        install()
        return
    units = managed_units()
    if action == "start":
        if len(units) != len(NAMES):
            raise RuntimeError("install the native EM units first")
        failed = [name for name in units if systemctl("show", name, "--property=ActiveState", "--value").stdout.strip() == "failed"]
        if failed:
            systemctl("reset-failed", *failed)
        start_services(units)
    elif action == "stop" and units:
        systemctl("stop", *units)
    status = []
    for name in units:
        properties = systemctl("show", name, "--property=ActiveState,SubState,MainPID,MemoryCurrent,NRestarts").stdout
        values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
        status.append({"service": name, **values})
    print(json.dumps({"native_services": status}))


if __name__ == "__main__":
    main()
