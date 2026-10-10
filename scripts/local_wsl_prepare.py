"""Prepare Linux path configuration and an isolated frontend dependency tree."""
import json
import os
import pwd
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "enterprise-mind"
WORK = ROOT / ".local/runtime/wsl"


def prepare() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("run as the Ubuntu development user")
    WORK.mkdir(parents=True, exist_ok=True)
    frontend = STATE / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", "tsconfig.json"):
        shutil.copyfile(ROOT / "frontend" / name, frontend / name)
    shutil.copyfile(ROOT / "frontend/vite.config.ts", frontend / "vite.base.config.ts")
    for name in ("src", "tests", "index.html", "public"):
        source = ROOT / "frontend" / name
        target = frontend / name
        if not source.exists():
            continue
        if target.is_symlink():
            if target.resolve() != source:
                raise RuntimeError("existing frontend source link mismatch")
        elif target.exists():
            raise RuntimeError("existing frontend source path is not a managed link")
        else:
            target.symlink_to(source, target_is_directory=source.is_dir())
    (frontend / "vite.config.ts").write_text(
        "import { defineConfig, mergeConfig } from 'vite';\n"
        "import base from './vite.base.config.ts';\n"
        "export default defineConfig(env => mergeConfig(typeof base === 'function' ? base(env) : base, {\n"
        "  resolve: { preserveSymlinks: true },\n"
        "  server: { watch: { usePolling: true, interval: 1000 } }\n"
        "}));\n", encoding="utf-8",
    )
    private = ROOT / ".local/runtime/secrets"
    model_private = ROOT / ".local/secrets"
    environment = {
        "PATH": f"{STATE}/tools/node-v24.16.0-linux-x64/bin:{STATE}/tools/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "backend/src"),
        "PYTHONPYCACHEPREFIX": str(STATE / "pycache"),
        "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "LANG": "C.UTF-8",
        "EM_ENV": "development", "EM_POSTGRES_HOST": "127.0.0.1",
        "EM_POSTGRES_PORT": "5433", "EM_POSTGRES_DATABASE": "enterprise_mind",
        "EM_POSTGRES_USER": "enterprise_mind_app",
        "EM_POSTGRES_PASSWORD_FILE": str(private / "postgres_app_password"),
        "EM_TOKEN_SIGNING_KEY_FILE": str(private / "token_signing_key"),
        "EM_APPLICATION_CONFIG": str(ROOT / ".local/runtime/application.yaml"),
        "EM_AGENT_CONFIG": str(ROOT / "config/agent.yaml"),
        "EM_DATABASE_CONFIG": str(ROOT / ".local/runtime/database.yaml"),
        "EM_CHECKPOINT_POSTGRES_USER": "enterprise_mind_checkpoint",
        "EM_CHECKPOINT_POSTGRES_PASSWORD_FILE": str(private / "postgres_checkpoint_password"),
        "EM_MINIO_ENDPOINT": "127.0.0.1:39000", "EM_MINIO_BUCKET": "enterprise-mind-local",
        "EM_MINIO_SECURE": "false",
        "EM_MINIO_ACCESS_KEY_FILE": str(private / "minio_app_access"),
        "EM_MINIO_SECRET_KEY_FILE": str(private / "minio_app_secret"),
        "EM_REDIS_HOST": "127.0.0.1", "EM_REDIS_PORT": "36379",
        "EM_REDIS_USERNAME": "enterprise_mind_app",
        "EM_REDIS_PASSWORD_FILE": str(private / "redis_app_password"),
        "EM_REDIS_STREAM": "enterprise-mind:local:processing",
        "EM_OFFICE_CONVERTER_TOKEN_FILE": str(private / "converter_token"),
        "EM_PROVIDERS_CONFIG": str(ROOT / "config/providers.local.yaml"),
        "EM_RAG_CONFIG": str(ROOT / "config/rag.local.yaml"),
    }
    for key, filename in {
        "EM_LLM_API_KEY_FILE": "llm_api_key.txt", "EM_VISION_API_KEY_FILE": "vision_api_key.txt",
        "EM_EMBEDDING_API_KEY_FILE": "embedding_api_key.txt", "EM_RERANK_API_KEY_FILE": "embedding_api_key.txt",
        "EM_OCR_SECRET_ID_FILE": "ocr_secret_id.txt", "EM_OCR_SECRET_KEY_FILE": "ocr_secret_key.txt",
    }.items():
        environment[key] = str(model_private / filename)
    if (WORK / "demo-source.json").is_file():
        environment["EM_DATABASE_CONFIG"] = str(WORK / "database.yaml")
        environment["EM_DATABASE_MCP_SERVER_CONFIG"] = str(WORK / "database-mcp-server.yaml")
    # Optional Linux-only local overrides, containing paths/URLs, never credentials.
    overrides = WORK / "environment.local.json"
    if overrides.exists():
        extra = json.loads(overrides.read_text(encoding="utf-8"))
        if not isinstance(extra, dict) or set(extra) - {"EM_MODEL_PROXY_URL"}:
            raise RuntimeError("unsupported Linux environment override")
        environment.update(extra)
    (WORK / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
    lines = []
    for key, value in environment.items():
        if not isinstance(value, str) or any(ch in value for ch in "\x00\n\r"):
            raise RuntimeError("invalid Linux environment value")
        lines.append(f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"')
    (WORK / "runtime.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (WORK / "paths.json").write_text(json.dumps({
        "user": pwd.getpwuid(os.geteuid()).pw_name,
        "state": str(STATE), "python": str(STATE / "backend-venv/bin/python"),
        "node": str(STATE / "tools/node-v24.16.0-linux-x64/bin/node"),
        "frontend": str(frontend), "source": str(ROOT),
    }, indent=2), encoding="utf-8")
    print(json.dumps({"linux_configuration_prepared": True, "credentials_printed": False}))


if __name__ == "__main__":
    prepare()
