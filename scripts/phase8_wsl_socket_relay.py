"""Relay one binary TCP stream between stdin/stdout and a WSL socket."""

from __future__ import annotations

import os
import socket
import sys
import threading
from contextlib import suppress


def _upload(sock: socket.socket) -> None:
    try:
        while chunk := os.read(sys.stdin.fileno(), 64 * 1024):
            sock.sendall(chunk)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_WR)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: phase8_wsl_socket_relay.py HOST PORT")
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5) as sock:
        sock.settimeout(None)
        thread = threading.Thread(target=_upload, args=(sock,), daemon=True)
        thread.start()
        try:
            while chunk := sock.recv(64 * 1024):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


if __name__ == "__main__":
    main()
