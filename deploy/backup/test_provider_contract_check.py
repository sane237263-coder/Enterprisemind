from __future__ import annotations

import hashlib

import pytest

from deploy.provider_contract_check import check_provider_contract


def _write_tokenizer(config_dir, relative_path: str, contents: bytes) -> str:
    path = config_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def _providers(llm_path: str, llm_hash: str, embedding_path: str, embedding_hash: str) -> str:
    return f"""
llm:
  tokenizer_file: '{llm_path}'
  tokenizer_sha256: '{llm_hash}'
embedding: {{tokenizer_file: \"{embedding_path}\", tokenizer_sha256: \"{embedding_hash}\"}}
"""


def test_accepts_quoted_relative_and_container_paths_and_returns_fixed_hash_keys(tmp_path):
    llm_hash = _write_tokenizer(tmp_path, "tokenizers/llm file.json", b"synthetic llm tokenizer")
    embedding_hash = _write_tokenizer(
        tmp_path, "tokenizers/embedding.json", b"synthetic embedding tokenizer"
    )
    rerank_hash = _write_tokenizer(
        tmp_path, "tokenizers/rerank.json", b"synthetic rerank tokenizer"
    )
    providers = (
        _providers(
            "tokenizers/llm file.json",
            llm_hash,
            "/run/config/tokenizers/embedding.json",
            embedding_hash,
        )
        + f"""
rerank:
  tokenizer_file: tokenizers/rerank.json
  tokenizer_sha256: '{rerank_hash}'
"""
    )

    errors, hashes = check_provider_contract(providers, tmp_path)

    assert errors == []
    assert hashes == {
        "tokenizer/llm": llm_hash,
        "tokenizer/embedding": embedding_hash,
        "tokenizer/rerank": rerank_hash,
    }


def test_llm_defaults_to_local_tokenizer_and_utf8_bytes_accepts_explicit_nulls(
    tmp_path,
):
    embedding_hash = _write_tokenizer(tmp_path, "embedding.json", b"synthetic embedding tokenizer")
    local_llm_hash = _write_tokenizer(tmp_path, "llm.json", b"synthetic llm tokenizer")
    providers = f"""
llm: {{tokenizer_file: llm.json, tokenizer_sha256: \"{local_llm_hash}\"}}
embedding:
  tokenizer_file: embedding.json
  tokenizer_sha256: '{embedding_hash}'
"""

    errors, hashes = check_provider_contract(providers, tmp_path)

    assert errors == []
    assert hashes["tokenizer/llm"] == local_llm_hash

    byte_budget = f"""
llm: {{token_counter: utf8_bytes, tokenizer_file: null}}
embedding: {{tokenizer_file: embedding.json, tokenizer_sha256: \"{embedding_hash}\"}}
"""
    errors, hashes = check_provider_contract(byte_budget, tmp_path)
    assert errors == []
    assert hashes == {"tokenizer/embedding": embedding_hash}

    configured_tokenizer = f"""
llm: {{token_counter: utf8_bytes, tokenizer_file: llm.json}}
embedding: {{tokenizer_file: embedding.json, tokenizer_sha256: \"{embedding_hash}\"}}
"""
    errors, hashes = check_provider_contract(configured_tokenizer, tmp_path)
    assert errors == ["llm byte token counter must not configure tokenizer files"]
    assert hashes == {"tokenizer/embedding": embedding_hash}


@pytest.mark.parametrize(
    "tokenizer_path",
    [
        "../.local/tokenizers/embedding/tokenizer.json",
        "/etc/passwd",
        "C:/private/tokenizer.json",
        r"C:\\private\\tokenizer.json",
    ],
)
def test_rejects_paths_outside_release_config_without_echoing_them(tmp_path, tokenizer_path):
    providers = f"""
llm: {{token_counter: utf8_bytes}}
embedding: {{tokenizer_file: '{tokenizer_path}', tokenizer_sha256: \"{"0" * 64}\"}}
"""

    errors, hashes = check_provider_contract(providers, tmp_path)

    assert errors == ["embedding tokenizer path must stay within the release config directory"]
    assert tokenizer_path not in " ".join(errors)
    assert hashes == {}


def test_diagnostics_do_not_echo_unrelated_paths_or_secret_values(tmp_path):
    private_path = "../.local/tokenizers/private file.json"
    secret_value = "synthetic-secret-marker"
    providers = f"""
llm: {{token_counter: utf8_bytes, api_key: {secret_value}}}
embedding: {{tokenizer_file: '{private_path}', tokenizer_sha256: \"{"0" * 64}\"}}
"""

    errors, hashes = check_provider_contract(providers, tmp_path)
    diagnostics = " ".join(errors)

    assert errors == ["embedding tokenizer path must stay within the release config directory"]
    assert private_path not in diagnostics
    assert secret_value not in diagnostics
    assert hashes == {}


def test_rejects_symlink_that_resolves_outside_release_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    outside = tmp_path / "outside-tokenizer.json"
    outside.write_bytes(b"synthetic outside tokenizer")
    link = config_dir / "tokenizer-link.json"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {type(exc).__name__}")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    providers = f"""
llm: {{token_counter: utf8_bytes}}
embedding: {{tokenizer_file: tokenizer-link.json, tokenizer_sha256: \"{digest}\"}}
"""

    errors, hashes = check_provider_contract(providers, config_dir)

    assert errors == ["embedding tokenizer path must stay within the release config directory"]
    assert hashes == {}


def test_rejects_missing_file_and_digest_mismatch(tmp_path):
    digest = _write_tokenizer(tmp_path, "present.json", b"synthetic actual bytes")
    providers = f"""
llm: {{token_counter: utf8_bytes}}
embedding: {{tokenizer_file: absent.json, tokenizer_sha256: \"{"0" * 64}\"}}
"""
    errors, hashes = check_provider_contract(providers, tmp_path)
    assert errors == ["embedding tokenizer file is missing or unreadable"]
    assert hashes == {}

    providers = f"""
llm: {{token_counter: utf8_bytes}}
embedding: {{tokenizer_file: present.json, tokenizer_sha256: \"{"0" * 64}\"}}
"""
    errors, hashes = check_provider_contract(providers, tmp_path)
    assert errors == ["embedding tokenizer SHA-256 does not match"]
    assert hashes == {}
    assert digest != "0" * 64


def test_reports_yaml_and_mapping_errors_as_controlled_failures(tmp_path):
    errors, hashes = check_provider_contract("llm: [unterminated", tmp_path)
    assert errors == ["provider configuration YAML is malformed"]
    assert hashes == {}

    errors, hashes = check_provider_contract("- llm\n- embedding\n", tmp_path)
    assert errors == ["provider configuration must be a mapping"]
    assert hashes == {}

    errors, hashes = check_provider_contract("llm: []\nembedding: {}\n", tmp_path)
    assert errors == [
        "llm provider configuration must be a mapping",
        "embedding tokenizer path and SHA-256 are required",
    ]
    assert hashes == {}


def test_requires_llm_and_embedding_providers(tmp_path):
    errors, hashes = check_provider_contract("{}", tmp_path)

    assert errors == ["llm provider is required", "embedding provider is required"]
    assert hashes == {}


def test_rejects_unsupported_token_counter_and_non_lowercase_digest(tmp_path):
    _write_tokenizer(tmp_path, "embedding.json", b"synthetic embedding tokenizer")
    providers = f"""
llm: {{token_counter: model_default}}
embedding: {{tokenizer_file: embedding.json, tokenizer_sha256: \"{"A" * 64}\"}}
"""

    errors, hashes = check_provider_contract(providers, tmp_path)

    assert errors == [
        "llm token_counter must be local_tokenizer or utf8_bytes",
        "embedding tokenizer SHA-256 must be lowercase hexadecimal",
    ]
    assert hashes == {}

    # Intentionally unquoted: YAML parses this all-numeric scalar as an int.
    unquoted_numeric_hash = f"""
llm: {{token_counter: utf8_bytes}}
embedding: {{tokenizer_file: embedding.json, tokenizer_sha256: {"0" * 64}}}
"""
    errors, hashes = check_provider_contract(unquoted_numeric_hash, tmp_path)
    assert errors == ["embedding tokenizer path and SHA-256 are required"]
    assert hashes == {}
