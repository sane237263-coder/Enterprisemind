"""Offline checks for provider tokenizer files included in a release config."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import yaml

if __package__:
    from .config_access_check import resolve_config_file
else:
    from config_access_check import resolve_config_file

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKENIZER_KEYS = ("tokenizer_file", "tokenizer_sha256")


def _release_path(raw: object) -> tuple[str, ...] | None:
    if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw or "\x00" in raw:
        return None

    path = PurePosixPath(raw)
    if path.is_absolute():
        if not raw.startswith("/run/config/"):
            return None
        path = PurePosixPath(raw[len("/run/config/") :])
        if path.is_absolute():
            return None
    if not path.parts or any(part in (".", "..") for part in path.parts):
        return None
    return path.parts


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_provider_contract(
    providers_text: str,
    config_dir: Path,
    *,
    providers_path: Path | None = None,
    tokenizer_paths: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Validate configured local tokenizer files without loading model data.

    Errors use fixed, capability-specific messages and never include source
    configuration values or filesystem paths.
    """
    errors: list[str] = []
    hashes: dict[str, str] = {}
    try:
        providers = yaml.safe_load(providers_text)
    except (yaml.YAMLError, TypeError, ValueError):
        return ["provider configuration YAML is malformed"], hashes
    if not isinstance(providers, Mapping):
        return ["provider configuration must be a mapping"], hashes

    try:
        config_root = config_dir.resolve(strict=True)
        if not config_root.is_dir():
            raise OSError
    except (OSError, RuntimeError, TypeError):
        return ["release config directory is unavailable"], hashes

    try:
        provider_base = (
            providers_path.resolve(strict=True).parent.relative_to(config_root).parts
            if providers_path is not None
            else ()
        )
    except (OSError, RuntimeError, ValueError):
        return ["provider YAML must stay within the release config directory"], hashes

    for capability in ("llm", "embedding", "rerank"):
        provider = providers.get(capability)
        if capability == "rerank" and provider is None:
            continue
        if provider is None:
            if capability in ("llm", "embedding"):
                errors.append(f"{capability} provider is required")
            continue
        if not isinstance(provider, Mapping):
            errors.append(f"{capability} provider configuration must be a mapping")
            continue

        if capability == "llm":
            token_counter = provider.get("token_counter", "local_tokenizer")
            if token_counter == "utf8_bytes":
                if any(provider.get(key) is not None for key in _TOKENIZER_KEYS):
                    errors.append("llm byte token counter must not configure tokenizer files")
                continue
            if token_counter != "local_tokenizer":
                errors.append("llm token_counter must be local_tokenizer or utf8_bytes")
                continue

        raw_path = provider.get("tokenizer_file")
        expected_hash = provider.get("tokenizer_sha256")
        if not isinstance(raw_path, str) or not raw_path or not isinstance(expected_hash, str):
            errors.append(f"{capability} tokenizer path and SHA-256 are required")
            continue
        if not _SHA256.fullmatch(expected_hash):
            errors.append(f"{capability} tokenizer SHA-256 must be lowercase hexadecimal")
            continue
        relative_parts = _release_path(raw_path)
        if relative_parts is None:
            errors.append(
                f"{capability} tokenizer path must stay within the release config directory"
            )
            continue

        try:
            # The product resolves relative tokenizer paths from the resolved
            # providers.yaml parent. Container-absolute paths use the mount root.
            mount_parts = (
                relative_parts
                if raw_path.startswith("/run/config/")
                else (*provider_base, *relative_parts)
            )
            candidate = resolve_config_file(config_root, mount_parts)
            if tokenizer_paths is not None:
                tokenizer_paths[f"tokenizer/{capability}"] = mount_parts
            actual_hash = _file_sha256(candidate)
        except ValueError:
            errors.append(
                f"{capability} tokenizer path must stay within the release config directory"
            )
            continue
        except (OSError, RuntimeError):
            errors.append(f"{capability} tokenizer file is missing or unreadable")
            continue
        if actual_hash != expected_hash:
            errors.append(f"{capability} tokenizer SHA-256 does not match")
            continue
        hashes[f"tokenizer/{capability}"] = actual_hash

    return errors, hashes
