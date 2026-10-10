"""Expose one WSL loopback TCP port on a Windows loopback port for local tests.

This is a local, reversible test helper.  It does not inspect or log the
forwarded bytes; each accepted connection is passed through ``wsl.exe`` to
the exact target port.  Use a dedicated PID and stop it after the test run.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _wsl_path(path: Path) -> str:
    drive = path.drive.rstrip(":").lower()
    return f"/mnt/{drive}{path.as_posix()[2:]}"


def _copy_socket_to_process(
    client: socket.socket, process: subprocess.Popen[bytes]
) -> None:
    assert process.stdin is not None
    try:
        while data := client.recv(64 * 1024):
            process.stdin.write(data)
            process.stdin.flush()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            process.stdin.close()


def _copy_process_to_socket(
    client: socket.socket, process: subprocess.Popen[bytes]
) -> None:
    assert process.stdout is not None
    read = getattr(process.stdout, "read1", process.stdout.read)
    try:
        while data := read(64 * 1024):
            client.sendall(data)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            client.shutdown(socket.SHUT_WR)


def _handle_client(
    client: socket.socket,
    distribution: str,
    target_host: str,
    target_port: int,
) -> None:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                "wsl.exe",
                "-d",
                distribution,
                "--exec",
                "python3",
                _wsl_path(Path(__file__).with_name("phase8_wsl_socket_relay.py")),
                target_host,
                str(target_port),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        threads = (
            threading.Thread(target=_copy_socket_to_process, args=(client, process)),
            threading.Thread(target=_copy_process_to_socket, args=(client, process)),
        )
        for thread in threads:
            thread.start()
        threads[0].join()
        threads[1].join(timeout=10)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        with contextlib.suppress(OSError):
            client.close()


def serve(
    listen_host: str,
    listen_port: int,
    distribution: str,
    target_host: str,
    target_port: int,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    with socket.create_server((listen_host, listen_port), backlog=64) as listener:
        address = listener.getsockname()
        if on_ready is not None:
            on_ready(
                f"listening {address[0]}:{address[1]} -> WSL {target_host}:{target_port}"
            )
        while True:
            client, _ = listener.accept()
            threading.Thread(
                target=_handle_client,
                args=(client, distribution, target_host, target_port),
                daemon=True,
            ).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()
    serve(
        args.listen_host,
        args.listen_port,
        args.distribution,
        args.target_host,
        args.target_port,
        print,
    )


if __name__ == "__main__":
    main()
