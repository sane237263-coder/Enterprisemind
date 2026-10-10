"""Run an EM CLI operation with the managed Linux environment."""
import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local/runtime/wsl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["app", "migrator"], default="app")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    paths = json.loads((WORK / "paths.json").read_text())
    environment = os.environ.copy()
    environment.update(json.loads((WORK / "environment.json").read_text()))
    environment.pop("EM_BOOTSTRAP_ADMIN_USERNAME", None)
    environment.pop("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", None)
    if options.role == "migrator":
        environment["EM_POSTGRES_USER"] = "enterprise_mind_migrator"
        environment["EM_POSTGRES_PASSWORD_FILE"] = str(ROOT / ".local/runtime/secrets/postgres_migrator_password")
    if options.bootstrap:
        identity = json.loads((ROOT / ".local/runtime/runtime.json").read_text())
        environment["EM_BOOTSTRAP_ADMIN_USERNAME"] = identity["admin_username"]
        environment["EM_BOOTSTRAP_ADMIN_PASSWORD_FILE"] = str(ROOT / ".local/runtime/secrets/bootstrap_admin_password")
    result = subprocess.run([paths["python"], "-m", "enterprise_mind.cli", *options.arguments],
                            env=environment, cwd=ROOT, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
