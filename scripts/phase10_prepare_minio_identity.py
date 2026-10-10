"""Create a run-scoped MinIO app identity without printing its Secret."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from pathlib import Path


def ensure(path: Path, value: str) -> bool:
    if path.exists():
        current = path.read_text(encoding="utf-8").strip()
        if not current:
            raise RuntimeError(f"refusing empty existing identity file: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    access = "em_p10_app_lm01"
    secret = secrets.token_urlsafe(48)
    access_file = args.directory / "minio_p10_app_access"
    secret_file = args.directory / "minio_p10_app_secret"
    access_created = ensure(access_file, access)
    secret_created = ensure(secret_file, secret)
    result = {
        "access_file": str(access_file),
        "secret_file": str(secret_file),
        "access_created": access_created,
        "secret_created": secret_created,
        "access_sha256": hashlib.sha256(access.encode()).hexdigest(),
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
