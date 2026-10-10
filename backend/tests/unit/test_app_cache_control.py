from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from enterprise_mind.app import create_app
from enterprise_mind.infrastructure.settings import APISettings


def test_request_context_preserves_explicit_cache_control_and_defaults_to_no_store(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("test-secret-12345678901234567890", encoding="utf-8")
    repository_root = Path(__file__).resolve().parents[3]
    app = create_app(
        APISettings(
            env="test",
            postgres_host="127.0.0.1",
            postgres_database="unused",
            postgres_user="unused",
            postgres_password_file=secret_file,
            token_signing_key_file=secret_file,
            application_config=repository_root / "config" / "application.yaml",
        )
    )

    @app.get("/test/cache-control/explicit")
    def explicit_cache_control() -> Response:
        return Response("private", headers={"Cache-Control": "private, no-store"})

    @app.get("/test/cache-control/default")
    def default_cache_control() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app)
    try:
        explicit = client.get("/test/cache-control/explicit")
        default = client.get("/test/cache-control/default")
    finally:
        client.close()

    assert explicit.status_code == 200
    assert explicit.headers["cache-control"] == "private, no-store"
    assert default.status_code == 200
    assert default.headers["cache-control"] == "no-store"
