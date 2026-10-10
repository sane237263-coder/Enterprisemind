"""Bounded Provider calls. No network request is made until a configured capability is used."""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Protocol

import httpx
from tokenizers import Tokenizer

from enterprise_mind.infrastructure.model_transport import model_proxy_url
from enterprise_mind.infrastructure.settings import ConfigurationError, read_secret
from enterprise_mind.rag.config import (
    EmbeddingConfig,
    EndpointConfig,
    LLMConfig,
    RerankProviderConfig,
)


class ProviderFailure(Exception):
    """Safe error with no URL, request body, response body or credentials."""


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class EmbeddingProvider(Protocol):
    """Text embedding capability independent of the model and service vendor."""

    model_key: str
    dimensions: int

    @property
    def counter(self) -> TokenCounter: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class RerankProvider(Protocol):
    """Document ranking capability independent of the model and service vendor."""

    model_key: str
    max_input_tokens: int

    @property
    def counter(self) -> TokenCounter: ...

    async def rerank(self, question: str, documents: list[str]) -> list[float]: ...


class LocalTokenizer:
    def __init__(self, path: Path, digest: str) -> None:
        try:
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("tokenizer digest mismatch")
            self.tokenizer = Tokenizer.from_str(content.decode("utf-8"))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        except Exception:
            raise ConfigurationError("模型 Tokenizer 文件缺失或未通过校验。") from None

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=True).ids)


class Utf8ByteCounter:
    """Conservative text budget estimate, not provider token usage or billing data."""

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


def llm_token_counter(config: LLMConfig) -> TokenCounter:
    if config.token_counter == "utf8_bytes":
        return Utf8ByteCounter()
    assert config.tokenizer_file and config.tokenizer_sha256
    return LocalTokenizer(Path(config.tokenizer_file), config.tokenizer_sha256)


def validate_vector(value: object, dimensions: int) -> list[float]:
    if not isinstance(value, list) or len(value) != dimensions:
        raise ProviderFailure("EMBEDDING_INVALID")
    if any(type(item) not in (int, float) for item in value):
        raise ProviderFailure("EMBEDDING_INVALID")
    try:
        vector = [float(item) for item in value]
    except (OverflowError, ValueError):
        raise ProviderFailure("EMBEDDING_INVALID") from None
    if not all(math.isfinite(item) and abs(item) <= 3.4028234e38 for item in vector):
        raise ProviderFailure("EMBEDDING_INVALID")
    if math.sqrt(sum(item * item for item in vector)) <= 1e-30:
        raise ProviderFailure("EMBEDDING_INVALID")
    return vector


class JSONProvider:
    def __init__(self, config: EndpointConfig, secret_env: str) -> None:
        self.config = config
        secret_path = os.environ.get(secret_env)
        if not secret_path:
            raise ConfigurationError("已启用的 Provider 缺少 Secret 文件配置。")
        self._key = read_secret(Path(secret_path))
        self.counter = LocalTokenizer(Path(config.tokenizer_file), config.tokenizer_sha256)
        self.model_key = config.model_key
        self.proxy = model_proxy_url()

    async def request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.config.max_retries + 1):
            retry = False
            try:
                async with (
                    asyncio.timeout(self.config.timeout_seconds),
                    httpx.AsyncClient(
                        timeout=self.config.timeout_seconds,
                        follow_redirects=False,
                        trust_env=False,
                        proxy=self.proxy,
                    ) as client,
                    client.stream(
                        "POST",
                        self.config.base_url.rstrip("/") + path,
                        headers={"Authorization": "Bearer " + self._key.get_secret_value()},
                        json=body,
                    ) as response,
                ):
                    if response.status_code in {408, 429, 500, 502, 503, 504}:
                        retry = True
                    elif response.status_code != 200:
                        raise ProviderFailure("PROVIDER_REJECTED")
                    else:
                        payload = bytearray()
                        async for part in response.aiter_bytes():
                            payload.extend(part)
                            if len(payload) > 8 * 1024 * 1024:
                                raise ProviderFailure("PROVIDER_RESPONSE_INVALID")
                        decoded = json.loads(payload)
                        if not isinstance(decoded, dict):
                            raise ValueError("invalid response")
                        return decoded
            except (httpx.TimeoutException, httpx.NetworkError, TimeoutError):
                retry = True
            except (ValueError, httpx.HTTPError):
                raise ProviderFailure("PROVIDER_RESPONSE_INVALID") from None
            if retry and attempt < self.config.max_retries:
                await asyncio.sleep(min(2.0, 0.25 * 2**attempt))
        raise ProviderFailure("PROVIDER_UNAVAILABLE")


class HTTPEmbeddings(JSONProvider):
    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config, config.secret_file_env)
        self.embedding_config = config
        self.dimensions = config.dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        config = self.embedding_config
        if not texts or any(
            not t.strip() or self.counter.count(t) > config.max_input_tokens for t in texts
        ):
            raise ProviderFailure("EMBEDDING_INPUT_INVALID")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), config.max_batch_size):
            batch = texts[start : start + config.max_batch_size]
            body: dict[str, Any] = {
                "model": config.model,
                "input": batch,
                "encoding_format": "float",
            }
            if config.send_dimensions:
                body["dimensions"] = config.dimensions
            result = await self.request("/embeddings", body)
            try:
                rows = result["data"]
                if (
                    not isinstance(rows, list)
                    or len(rows) != len(batch)
                    or any(type(row["index"]) is not int for row in rows)
                    or sorted(row["index"] for row in rows) != list(range(len(batch)))
                ):
                    raise ValueError("invalid embedding indices")
                vectors.extend(
                    validate_vector(row["embedding"], self.dimensions)
                    for row in sorted(rows, key=lambda row: row["index"])
                )
            except (KeyError, TypeError, ValueError):
                raise ProviderFailure("EMBEDDING_INVALID") from None
        return vectors


class HTTPReranker(JSONProvider):
    def __init__(self, config: RerankProviderConfig) -> None:
        super().__init__(config, config.secret_file_env)
        self.rerank_config = config
        self.max_input_tokens = config.max_input_tokens

    async def rerank(self, question: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        if any(
            self.counter.count(question + "\n" + doc) > self.max_input_tokens for doc in documents
        ):
            raise ProviderFailure("RERANK_INPUT_INVALID")
        config = self.rerank_config
        result = await self.request(
            "/rerank",
            {
                "model": config.model,
                "query": question,
                "documents": documents,
                "top_n": len(documents),
                "return_documents": False,
            },
        )
        try:
            rows = result["results"]
            if (
                not isinstance(rows, list)
                or len(rows) != len(documents)
                or any(type(row["index"]) is not int for row in rows)
                or sorted(row["index"] for row in rows) != list(range(len(documents)))
            ):
                raise ValueError("invalid rerank indices")
            scores = []
            for row in sorted(rows, key=lambda row: row["index"]):
                score = row["relevance_score"]
                if (
                    type(score) not in (int, float)
                    or not math.isfinite(score)
                    or not config.score_min <= score <= config.score_max
                ):
                    raise ValueError("invalid rerank score")
                scores.append(float(score))
            return scores
        except (KeyError, TypeError, ValueError):
            raise ProviderFailure("RERANK_INVALID") from None
