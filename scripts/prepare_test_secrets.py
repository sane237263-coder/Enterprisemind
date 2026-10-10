"""Create private, ignored test secrets once; never print or overwrite their contents."""

import hashlib
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1] / ".local" / "test-secrets"
root.mkdir(parents=True, exist_ok=True)
for name in (
    "postgres_password",
    "business_password",
    "token_key",
    "admin_password",
    "redis_password",
    "redis_test_admin",
    "minio_root_access",
    "minio_root_secret",
    "minio_app_access",
    "minio_app_secret",
    # Dedicated Phase-10 business-source identity; it is never shared with
    # the maintenance owner used to create the synthetic fixture.
    "phase10_business_readonly_password",
    "phase10_mcp_token",
):
    path = root / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        continue
    with os.fdopen(fd, "w", encoding="utf-8") as secret_file:
        secret_file.write(
            secrets.token_hex(12)
            if name.endswith("_access")
            else secrets.token_urlsafe(48)
        )
app_digest = hashlib.sha256((root / "redis_password").read_bytes()).hexdigest()
admin_digest = hashlib.sha256((root / "redis_test_admin").read_bytes()).hexdigest()
acl = (
    "user default off\n"
    f"user enterprise_mind_app on #{app_digest} ~enterprise-mind:* &enterprise-mind:* "
    "+ping +xgroup +xadd +xreadgroup +xautoclaim +xack +xdel +publish +subscribe +unsubscribe\n"
    f"user test_admin on #{admin_digest} ~* +@all\n"
)
(root / "redis.acl").write_text(acl, encoding="utf-8")
print(
    "Test secret files are ready under .local/test-secrets; values are not displayed."
)
