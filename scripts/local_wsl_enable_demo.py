"""Enable the explicitly selected demo database in the local Linux runtime."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local/runtime/wsl"
PRIVATE = ROOT / ".local/runtime/secrets"

if not (PRIVATE / "database_readonly_password").is_file():
    raise RuntimeError("prepare runtime Secrets first")
database = {
    "host": "127.0.0.1", "port": 5433, "database_name": "demo_db",
    "user": "enterprise_mind_db_readonly",
    "password_file": str(PRIVATE / "database_readonly_password"),
    "sslmode": "disable", "secret_ref": "secret:em_local_demo_readonly",
}
server = {
    "listen_host": "127.0.0.1", "listen_port": 38001,
    "allowed_hosts": ["127.0.0.1:38001", "localhost:38001"], "allowed_origins": [],
    "auth_secret_file": str(PRIVATE / "database_mcp_token"), "database": database,
}
client = {"database_mcp": {
    "endpoint": "http://127.0.0.1:38001/mcp",
    "auth_secret_file": str(PRIVATE / "database_mcp_token"),
}}
WORK.mkdir(parents=True, exist_ok=True)
(WORK / "database-mcp-server.yaml").write_text(json.dumps(server, indent=2))
(WORK / "database.yaml").write_text(json.dumps(client, indent=2))
(WORK / "demo-source.json").write_text(json.dumps({
    "name": "Ubuntu demo_db（演示数据）", "host": "127.0.0.1", "port": 5433,
    "database_name": "demo_db", "ssl_mode": "disable", "secret_ref": database["secret_ref"],
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"demo_database_configured": True, "mcp_port": 38001}))
