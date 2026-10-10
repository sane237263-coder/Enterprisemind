from __future__ import annotations

import os
import signal
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import config_access_check as checker


def _symlink_or_skip(link: Path, target: str | Path, *, target_is_directory: bool = False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {type(exc).__name__}")


def test_resolves_internal_relative_symlinks_and_dotdot_components(tmp_path):
    config = tmp_path / "config"
    data = config / "data"
    (data / "subdir").mkdir(parents=True)
    expected = data / "settings.yaml"
    expected.write_text("synthetic config", encoding="utf-8")
    _symlink_or_skip(config / "alias", "data/subdir/..", target_is_directory=True)

    resolved = checker.resolve_config_file(config, ("alias", "settings.yaml"))
    normalized = checker.resolve_config_file(config, ("data", "subdir", "..", "settings.yaml"))

    assert resolved == expected
    assert normalized == expected


@pytest.mark.parametrize(
    "parts",
    [
        ("..", "private.yaml"),
        ("nested", "..", "..", "private.yaml"),
        ("nested/file.yaml",),
        ("C:private.yaml",),
        ("",),
    ],
)
def test_rejects_invalid_relative_parts_without_echoing_them(tmp_path, parts):
    config = tmp_path / "config"
    config.mkdir()
    (config / "nested").mkdir()

    with pytest.raises(ValueError) as error:
        checker.resolve_config_file(config, parts)

    assert str(error.value) == "config file path is invalid"
    assert str(tmp_path) not in str(error.value)


def test_rejects_absolute_symlink_targets_even_when_target_is_inside_root(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    target = config / "settings.yaml"
    target.write_text("synthetic config", encoding="utf-8")
    _symlink_or_skip(config / "alias.yaml", target)

    with pytest.raises(ValueError, match="^config file path is invalid$"):
        checker.resolve_config_file(config, ("alias.yaml",))


def test_rejects_escaping_broken_looping_and_nonregular_targets(tmp_path):
    config = tmp_path / "config"
    directory = config / "nested"
    directory.mkdir(parents=True)
    _symlink_or_skip(directory / "escape.yaml", "../../outside.yaml")
    _symlink_or_skip(config / "broken.yaml", "missing.yaml")
    _symlink_or_skip(config / "loop-a", "loop-b")
    _symlink_or_skip(config / "loop-b", "loop-a")
    (config / "folder").mkdir()

    with pytest.raises(ValueError, match="^config file path is invalid$"):
        checker.resolve_config_file(config, ("nested", "escape.yaml"))
    for name in ("broken.yaml", "loop-a", "folder"):
        with pytest.raises(OSError, match="^config file is unavailable$"):
            checker.resolve_config_file(config, (name,))


def _mock_dropped_child(monkeypatch, *, groups=(), file_open_fails=False):
    calls = []
    opened = []
    reads = []
    closed = []

    monkeypatch.setattr(
        checker.os, "setgroups", lambda value: calls.append(("groups", value)), raising=False
    )
    monkeypatch.setattr(
        checker.os, "setgid", lambda value: calls.append(("gid", value)), raising=False
    )
    monkeypatch.setattr(
        checker.os, "setuid", lambda value: calls.append(("uid", value)), raising=False
    )
    monkeypatch.setattr(checker.os, "getgid", lambda: 65532, raising=False)
    monkeypatch.setattr(checker.os, "getegid", lambda: 65532, raising=False)
    monkeypatch.setattr(checker.os, "getuid", lambda: 65532, raising=False)
    monkeypatch.setattr(checker.os, "geteuid", lambda: 65532, raising=False)
    monkeypatch.setattr(checker.os, "getgroups", lambda: list(groups), raising=False)
    monkeypatch.setattr(checker.os, "getresgid", lambda: (65532, 65532, 65532), raising=False)
    monkeypatch.setattr(checker.os, "getresuid", lambda: (65532, 65532, 65532), raising=False)
    monkeypatch.setattr(
        checker.os, "fchdir", lambda fd: calls.append(("fchdir", fd)), raising=False
    )
    monkeypatch.setattr(checker.os, "close", lambda fd: closed.append(fd), raising=False)
    monkeypatch.setattr(checker.os, "O_NONBLOCK", 0x800, raising=False)

    def fake_open(path, flags):
        opened.append((path, flags))
        if path == "/proc/self/status":
            return 31
        if file_open_fails:
            raise PermissionError
        return 32

    def fake_read(fd, size):
        reads.append(fd)
        return b"Name:\tprobe\nCapEff:\t0000000000000000\n" if len(reads) == 1 else b""

    monkeypatch.setattr(checker.os, "open", fake_open)
    monkeypatch.setattr(checker.os, "read", fake_read, raising=False)
    monkeypatch.setattr(
        checker.os,
        "fstat",
        lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o400),
    )
    return calls, opened, reads, closed


def test_child_drops_identity_and_groups_then_opens_without_reading_config_bytes(monkeypatch):
    calls, opened, reads, closed = _mock_dropped_child(monkeypatch)

    status = checker._run_access_probe(29, [("settings", "settings-link.yaml")])

    assert status == 0
    assert calls[:3] == [("groups", []), ("gid", 65532), ("uid", 65532)]
    assert calls[-1] == ("fchdir", 29)
    assert opened[0][0] == "/proc/self/status"
    assert opened[1] == ("settings-link.yaml", os.O_RDONLY | os.O_NONBLOCK)
    assert reads == [31, 31]
    assert closed == [31, 29, 32]


@pytest.mark.parametrize(
    ("groups", "file_open_fails", "expected_status"),
    [([], True, 1), ([7], False, checker._SETUP_FAILURE)],
)
def test_child_fails_closed_for_unreadable_file_or_unexpected_groups(
    monkeypatch, groups, file_open_fails, expected_status
):
    _mock_dropped_child(monkeypatch, groups=groups, file_open_fails=file_open_fails)

    status = checker._run_access_probe(29, [("settings", "settings-link.yaml")])

    assert status == expected_status


class _SimulatedChildExit(Exception):
    pass


def test_check_access_passes_original_validated_spelling_to_child(tmp_path, monkeypatch):
    config = tmp_path / "config"
    restricted = config / "restricted"
    restricted.mkdir(parents=True)
    (config / "actual.yaml").write_text("synthetic config", encoding="utf-8")
    _symlink_or_skip(restricted / "back.yaml", "../actual.yaml")
    _symlink_or_skip(config / "settings-link.yaml", "restricted/back.yaml")
    child_files = []

    monkeypatch.setattr(checker.sys, "platform", "linux")
    monkeypatch.setattr(checker.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(checker.os, "O_PATH", 0x200000, raising=False)
    monkeypatch.setattr(checker.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setattr(checker.os, "open", lambda path, flags: 55)
    monkeypatch.setattr(checker.os, "fork", lambda: 0, raising=False)
    monkeypatch.setattr(
        checker,
        "_run_access_probe",
        lambda root_fd, files: child_files.extend(files) or 0,
    )

    def simulate_exit(status):
        raise _SimulatedChildExit(status)

    monkeypatch.setattr(checker.os, "_exit", simulate_exit)
    monkeypatch.setattr(checker.os, "close", lambda fd: None)

    with pytest.raises(_SimulatedChildExit) as error:
        checker.check_config_access(config, {"settings": ("settings-link.yaml",)})

    assert error.value.args == (0,)
    assert child_files == [("settings", "settings-link.yaml")]


@pytest.mark.parametrize(
    "status_content",
    [b"Name:\tprobe\nCapEff:\t0000000000000001\n", b"Name:\tprobe\n"],
)
def test_capability_guard_rejects_nonzero_or_unavailable_cap_eff(monkeypatch, status_content):
    read_count = 0

    monkeypatch.setattr(checker.os, "open", lambda path, flags: 31)
    monkeypatch.setattr(checker.os, "close", lambda fd: None)

    def fake_read(fd, size):
        nonlocal read_count
        read_count += 1
        return status_content if read_count == 1 else b""

    monkeypatch.setattr(checker.os, "read", fake_read, raising=False)

    assert not checker._has_no_effective_capabilities()


def test_timed_out_probe_kills_and_reaps_only_its_child(monkeypatch):
    child_pid = 4321
    wait_calls = []
    monotonic_values = iter((0.0, 0.0, 5.1))
    kill_signal = getattr(signal, "SIGKILL", 9)

    monkeypatch.setattr(checker.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(checker.signal, "SIGKILL", kill_signal, raising=False)

    def fake_waitpid(pid, options):
        wait_calls.append((pid, options))
        return (0, 0) if options == checker.os.WNOHANG else (pid, 0)

    monkeypatch.setattr(checker.os, "waitpid", fake_waitpid, raising=False)
    monkeypatch.setattr(checker.os, "kill", lambda pid, sig: wait_calls.append((pid, sig)))
    monkeypatch.setattr(checker.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(checker.time, "sleep", lambda seconds: None)

    errors = checker._wait_for_probe(child_pid, [("settings", "settings.yaml")])

    assert errors == ["UID/GID 65532 config access probe timed out"]
    assert wait_calls == [
        (child_pid, checker.os.WNOHANG),
        (child_pid, kill_signal),
        (child_pid, 0),
    ]
