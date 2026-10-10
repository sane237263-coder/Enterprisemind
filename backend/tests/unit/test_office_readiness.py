import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from enterprise_mind import office_converter


def test_missing_engine_is_not_ready_but_service_is_live(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("private-test-token")
    with TestClient(
        office_converter.create_converter(secret, executable=str(tmp_path / "missing"))
    ) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.text == "not ready"
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/convert/doc", content=b"fake").status_code == 401


@pytest.mark.parametrize(
    ("code", "output", "expected"),
    [
        (0, b"LibreOffice 26.2.6\n", True),
        (1, b"LibreOffice error", False),
        (0, b"another program", False),
    ],
)
async def test_readiness_executes_engine_with_private_workspace(
    tmp_path, monkeypatch, code, output, expected
):
    process = Mock(pid=123, returncode=code)
    process.communicate = AsyncMock(return_value=(output, None))
    process.wait = AsyncMock()
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(office_converter.asyncio, "create_subprocess_exec", launch)
    if office_converter.os.name == "posix":
        monkeypatch.setattr(office_converter.os, "killpg", Mock())
    monkeypatch.setenv("EM_LLM_API_KEY", "must-not-reach-engine")
    assert await office_converter.converter_ready("/usr/bin/libreoffice") is expected
    call = launch.call_args
    assert call.args[0] == "/usr/bin/libreoffice"
    assert call.args[-2:] == ("--headless", "--version")
    assert "EM_LLM_API_KEY" not in call.kwargs["env"]
    assert not call.kwargs["cwd"].exists()
    process.wait.assert_awaited_once()


async def test_readiness_kills_and_reaps_timed_out_probe(monkeypatch):
    process = Mock(pid=123, returncode=None)

    async def hang():
        await asyncio.Event().wait()

    process.communicate = hang
    process.wait = AsyncMock()
    monkeypatch.setattr(
        office_converter.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(office_converter, "READINESS_TIMEOUT_SECONDS", 0.01)
    kill = Mock()
    if office_converter.os.name == "posix":
        monkeypatch.setattr(office_converter.os, "killpg", kill)
    else:
        process.kill = kill
    assert await office_converter.converter_ready("/usr/bin/libreoffice") is False
    kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_unwritable_workspace_is_not_ready(monkeypatch):
    monkeypatch.setattr(office_converter, "TemporaryDirectory", Mock(side_effect=PermissionError))
    launch = AsyncMock()
    monkeypatch.setattr(office_converter.asyncio, "create_subprocess_exec", launch)
    assert await office_converter.converter_ready("/usr/bin/libreoffice") is False
    launch.assert_not_awaited()


def test_readiness_recovers_after_engine_failure(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("private-test-token")
    monkeypatch.setattr(office_converter, "converter_ready", AsyncMock(side_effect=[False, True]))
    with TestClient(office_converter.create_converter(secret)) as client:
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/ready").status_code == 200
