"""Resolve config files inside a mount root and probe runtime DAC access."""

from __future__ import annotations

import os
import signal
import stat
import sys
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path, PurePath, PureWindowsPath

_RUNTIME_UID = 65532
_RUNTIME_GID = 65532
_MAX_SYMLINKS = 40
_MAX_STATUS_FILES = 249
_SETUP_FAILURE = 250
_PROBE_TIMEOUT_SECONDS = 5.0
_PROBE_POLL_SECONDS = 0.01
_MAX_PROC_STATUS_BYTES = 65536


def _config_root(config_dir: Path) -> Path:
    try:
        root = Path(config_dir).resolve(strict=True)
        if not root.is_dir():
            raise OSError
    except (OSError, RuntimeError, TypeError, ValueError):
        raise OSError("config directory is unavailable") from None
    return root


def _validated_parts(relative_parts: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(relative_parts, tuple) or not relative_parts:
        raise ValueError("config file path is invalid")
    parts: list[str] = []
    for part in relative_parts:
        if (
            not isinstance(part, str)
            or not part
            or "/" in part
            or "\\" in part
            or ":" in part
            or "\x00" in part
        ):
            raise ValueError("config file path is invalid")
        parts.append(part)
    return tuple(parts)


def _link_parts(target: str) -> tuple[str, ...]:
    if (
        not isinstance(target, str)
        or not target
        or "\x00" in target
        or os.path.isabs(target)
        or PureWindowsPath(target).is_absolute()
        or bool(PureWindowsPath(target).drive)
    ):
        raise ValueError("config file path is invalid")
    return tuple(PurePath(target).parts)


def resolve_config_file(config_dir: Path, relative_parts: tuple[str, ...]) -> Path:
    """Return a regular file reached within the resolved config mount root.

    Relative symlinks are expanded one component at a time.  This deliberately
    rejects absolute link targets even when their current host target is below
    the config directory: the same link would point elsewhere after mounting.
    """
    root = _config_root(config_dir)
    pending = deque(_validated_parts(relative_parts))
    resolved: list[str] = []
    link_count = 0

    while pending:
        part = pending.popleft()
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ValueError("config file path is invalid")
            resolved.pop()
            continue

        candidate = root.joinpath(*resolved, part)
        try:
            metadata = candidate.lstat()
        except OSError:
            raise OSError("config file is unavailable") from None

        if stat.S_ISLNK(metadata.st_mode):
            link_count += 1
            if link_count > _MAX_SYMLINKS:
                raise OSError("config file is unavailable")
            try:
                target_parts = _link_parts(os.readlink(candidate))
            except ValueError:
                raise
            except OSError:
                raise OSError("config file is unavailable") from None
            pending.extendleft(reversed(target_parts))
            continue

        if pending and not stat.S_ISDIR(metadata.st_mode):
            raise OSError("config file is unavailable")
        resolved.append(part)

    if not resolved:
        raise OSError("config file is unavailable")
    result = root.joinpath(*resolved)
    try:
        metadata = result.lstat()
    except OSError:
        raise OSError("config file is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("config file is unavailable")
    return result


def _run_access_probe(root_fd: int, relative_files: list[tuple[str, str]]) -> int:
    """Run in the forked child; return 0, a failed-file index, or setup status."""
    try:
        os.setgroups([])
        os.setgid(_RUNTIME_GID)
        os.setuid(_RUNTIME_UID)
        if (
            os.getgid() != _RUNTIME_GID
            or os.getegid() != _RUNTIME_GID
            or os.getuid() != _RUNTIME_UID
            or os.geteuid() != _RUNTIME_UID
            or os.getgroups() != []
            or os.getresgid() != (_RUNTIME_GID, _RUNTIME_GID, _RUNTIME_GID)
            or os.getresuid() != (_RUNTIME_UID, _RUNTIME_UID, _RUNTIME_UID)
            or not _has_no_effective_capabilities()
        ):
            return _SETUP_FAILURE
        os.fchdir(root_fd)
        os.close(root_fd)
    except BaseException:
        return _SETUP_FAILURE

    for index, (_label, relative_path) in enumerate(relative_files, start=1):
        try:
            file_fd = os.open(relative_path, os.O_RDONLY | os.O_NONBLOCK)
        except BaseException:
            return index
        readable = True
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                readable = False
        except BaseException:
            readable = False
        try:
            os.close(file_fd)
        except BaseException:
            readable = False
        if not readable:
            return index
    return 0


def _has_no_effective_capabilities() -> bool:
    """Require the dropped child to have no effective Linux capabilities."""
    status_fd = -1
    readable = True
    try:
        status_fd = os.open(
            "/proc/self/status",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        content = bytearray()
        while len(content) <= _MAX_PROC_STATUS_BYTES:
            chunk = os.read(status_fd, 4096)
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_PROC_STATUS_BYTES:
            readable = False
    except BaseException:
        readable = False
    finally:
        if status_fd >= 0:
            try:
                os.close(status_fd)
            except BaseException:
                readable = False
    if not readable:
        return False

    for line in content.splitlines():
        if line.startswith(b"CapEff:"):
            try:
                return int(line.split(b":", 1)[1].strip(), 16) == 0
            except (ValueError, IndexError):
                return False
    return False


def _stop_and_reap(child_pid: int) -> None:
    try:
        os.kill(child_pid, signal.SIGKILL)
    except OSError:
        pass
    while True:
        try:
            os.waitpid(child_pid, 0)
            return
        except InterruptedError:
            continue
        except (ChildProcessError, OSError):
            return


def _wait_for_probe(child_pid: int, relative_files: list[tuple[str, str]]) -> list[str]:
    labels = [label for label, _path in relative_files]
    try:
        deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
        while True:
            if time.monotonic() >= deadline:
                _stop_and_reap(child_pid)
                return ["UID/GID 65532 config access probe timed out"]
            try:
                waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
            except InterruptedError:
                continue
            if waited_pid == child_pid:
                if not os.WIFEXITED(status):
                    return ["UID/GID 65532 config access probe failed"]
                code = os.WEXITSTATUS(status)
                if code == 0:
                    return []
                if 1 <= code <= len(labels):
                    return [f"{labels[code - 1]} config file is not readable by UID/GID 65532"]
                return ["UID/GID 65532 config access probe could not complete"]
            time.sleep(_PROBE_POLL_SECONDS)
    except BaseException:
        _stop_and_reap(child_pid)
        raise


def check_config_access(config_dir: Path, files: dict[str, tuple[str, ...]]) -> list[str]:
    """Check mount-contained config file readability as Linux UID/GID 65532.

    The caller remains unchanged.  The only identity change happens in a
    bounded forked child, and the file contents are never read or returned.
    """
    if not sys.platform.startswith("linux"):
        return ["config access checks require Linux"]
    if not isinstance(files, Mapping) or any(
        not isinstance(label, str) or not label or "\x00" in label or "\n" in label
        for label in files
    ):
        return ["config access check arguments are invalid"]
    if not files:
        return ["config access check requires files"]
    try:
        if os.geteuid() != 0:
            return ["config access checks require root"]
    except (AttributeError, OSError):
        return ["config access checks require root"]

    try:
        root = _config_root(config_dir)
    except OSError:
        return ["config directory is unavailable"]

    errors: list[str] = []
    relative_files: list[tuple[str, str]] = []
    for label, parts in files.items():
        try:
            original_parts = _validated_parts(parts)
            resolve_config_file(root, original_parts)
        except (OSError, ValueError):
            errors.append(f"{label} config file is invalid or unavailable")
            continue
        relative_files.append((label, "/".join(original_parts)))

    if not relative_files:
        return errors
    if len(relative_files) > _MAX_STATUS_FILES:
        return errors + ["config access check file inventory is too large"]

    if not hasattr(os, "O_PATH"):
        return errors + ["config directory cannot be opened for UID/GID access check"]
    root_flags = os.O_PATH | os.O_DIRECTORY
    try:
        root_fd = os.open(root, root_flags)
    except OSError:
        return errors + ["config directory cannot be opened for UID/GID access check"]

    try:
        try:
            child_pid = os.fork()
        except OSError:
            return errors + ["UID/GID 65532 config access probe could not start"]
        if child_pid < 0:
            return errors + ["UID/GID 65532 config access probe could not start"]
        if child_pid == 0:
            os._exit(_run_access_probe(root_fd, relative_files))
            return errors + ["UID/GID 65532 config access probe could not complete"]
        return errors + _wait_for_probe(child_pid, relative_files)
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass
