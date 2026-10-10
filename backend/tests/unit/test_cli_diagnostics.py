import json
import sys

import pytest
from pydantic import BaseModel, ValidationError

from enterprise_mind import cli
from enterprise_mind.infrastructure.settings import ConfigurationError

PRIVATE = "PRIVATE_PASSWORD_SQL_OR_PROVIDER_BODY"


@pytest.mark.parametrize("kind", ["configuration", "validation", "unexpected"])
def test_cli_failure_locates_errors_without_exposing_values(monkeypatch, capsys, kind):
    class Value(BaseModel):
        number: int

    def fail_settings():
        try:
            if kind == "validation":
                Value(number=PRIVATE)
            raise OSError("/private/secrets/" + PRIVATE)
        except (OSError, ValidationError) as error:
            if kind == "configuration":
                raise ConfigurationError(PRIVATE) from None
            if kind == "unexpected":
                raise RuntimeError(PRIVATE) from error
            raise

    monkeypatch.setattr(sys, "argv", ["enterprise-mind", "worker", "--once"])
    monkeypatch.setattr(cli, "DatabaseSettings", fail_settings)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert PRIVATE not in captured.err and "/private/secrets/" not in captured.err
    assert "Traceback" not in captured.err
    line = next(line for line in captured.err.splitlines() if line.startswith("cli_failure "))
    report = json.loads(line.removeprefix("cli_failure "))
    assert report["command"] == "worker"
    expected = {
        "configuration": "ConfigurationError",
        "validation": "ValidationError",
        "unexpected": "RuntimeError",
    }[kind]
    assert report["errors"][0]["exception_type"] == expected
    location = report["errors"][0]["stack_location"]
    if kind == "validation":
        assert location.startswith("main.py:") and location.endswith(":__init__")
    else:
        assert "test_cli_diagnostics.py:" in location
    if kind != "validation":
        assert report["errors"][1]["exception_type"] == "OSError"
    assert all(set(item) == {"exception_type", "stack_location"} for item in report["errors"])


def test_cli_diagnostic_bounds_cyclic_exception_context(capsys):
    error = RuntimeError(PRIVATE)
    error.__context__ = error
    cli._report_failure("worker", error)
    report = json.loads(capsys.readouterr().err.removeprefix("cli_failure "))
    assert len(report["errors"]) == 1
    assert PRIVATE not in json.dumps(report)
