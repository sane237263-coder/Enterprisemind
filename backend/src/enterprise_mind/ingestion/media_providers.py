"""Configured OCR/Vision adapters. Provider text is untrusted derived source data."""

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from enterprise_mind.infrastructure.model_transport import model_proxy_url
from enterprise_mind.infrastructure.settings import ConfigurationError, read_secret
from enterprise_mind.ingestion.media_retry import failure_category, retry_delay
from enterprise_mind.prompts import load_prompt
from enterprise_mind.rag.config import MediaProviderConfig, OCRProviderConfig, VisionProviderConfig
from enterprise_mind.rag.providers import ProviderFailure


class MediaResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class Box(MediaResult):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)


class OCRBlock(MediaResult):
    text: str = Field(min_length=1, max_length=100000)
    box: Box
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class OCRResult(MediaResult):
    blocks: list[OCRBlock] = Field(max_length=10000)
    tables: list["OCRTable"] = Field(default_factory=list, max_length=1000)


class OCRTable(MediaResult):
    rows: list[list[str]] = Field(min_length=1, max_length=10000)
    box: Box
    merged_ranges: list[str] = Field(default_factory=list, max_length=10000)


OCRResult.model_rebuild()


class VisionResult(MediaResult):
    description: str = Field(min_length=1, max_length=20000)
    image_type: Literal["diagram", "flowchart", "chart", "screenshot", "photo", "other"]
    uncertainties: list[str] = Field(default_factory=list, max_length=20)


class OCRProvider(Protocol):
    model_key: str

    async def recognize(self, image: bytes, width: int, height: int) -> OCRResult: ...


class VisionProvider(Protocol):
    model_key: str

    async def describe(self, image: bytes, width: int, height: int) -> VisionResult: ...


class MediaHTTP:
    def __init__(self, config: MediaProviderConfig, secret_env: str) -> None:
        self.config, self.model_key = config, config.model_key
        secret = os.environ.get(secret_env)
        if not secret:
            raise ConfigurationError("已启用的 OCR/Vision 缺少 Secret 文件配置。")
        self._key = read_secret(Path(secret))
        self.proxy = model_proxy_url()

    async def request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.config.max_retries + 1):
            unavailable_code = "MEDIA_PROVIDER_UNAVAILABLE"
            delay = retry_delay(attempt, None)
            try:
                async with (
                    asyncio.timeout(self.config.timeout_seconds),
                    httpx.AsyncClient(
                        timeout=self.config.timeout_seconds,
                        trust_env=False,
                        follow_redirects=False,
                        proxy=self.proxy,
                    ) as client,
                    client.stream(
                        "POST",
                        self.config.base_url.rstrip("/") + path,
                        headers={"Authorization": "Bearer " + self._key.get_secret_value()},
                        json=body,
                    ) as response,
                ):
                    if response.status_code != 200:
                        unavailable_code, retryable = await failure_category(response)
                        if not retryable:
                            raise ProviderFailure(unavailable_code)
                        delay = retry_delay(attempt, response.headers.get("Retry-After"))
                    else:
                        content = bytearray()
                        async for part in response.aiter_bytes():
                            content.extend(part)
                            if len(content) > 4 * 1024 * 1024:
                                raise ProviderFailure("MEDIA_RESPONSE_INVALID")
                        result = json.loads(content)
                        if not isinstance(result, dict):
                            raise ValueError("invalid response")
                        return result
            except (httpx.TimeoutException, httpx.NetworkError, TimeoutError):
                unavailable_code = "MEDIA_PROVIDER_UNAVAILABLE"
            except (ValueError, httpx.HTTPError):
                raise ProviderFailure("MEDIA_RESPONSE_INVALID") from None
            if attempt >= self.config.max_retries or delay is None:
                raise ProviderFailure(unavailable_code)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")


class HTTPOCR(MediaHTTP):
    def __init__(self, config: OCRProviderConfig) -> None:
        super().__init__(config, config.secret_file_env)

    async def recognize(self, image: bytes, width: int, height: int) -> OCRResult:
        raw = await self.request(
            "/ocr",
            {
                "model": self.config.model,
                "image_base64": base64.b64encode(image).decode("ascii"),
                "mime_type": "image/png",
                "width": width,
                "height": height,
            },
        )
        try:
            result = OCRResult.model_validate(raw)
            if any(
                block.box.x + block.box.width > width
                or block.box.y + block.box.height > height
                or not block.text.strip()
                for block in result.blocks
            ):
                raise ValueError("OCR bounds")
            if any(
                table.box.x + table.box.width > width
                or table.box.y + table.box.height > height
                or not table.rows[0]
                or any(len(row) != len(table.rows[0]) for row in table.rows)
                for table in result.tables
            ):
                raise ValueError("OCR table bounds")
            return result
        except ValueError:
            raise ProviderFailure("OCR_RESPONSE_INVALID") from None


class HTTPVision(MediaHTTP):
    def __init__(self, config: VisionProviderConfig) -> None:
        super().__init__(config, config.secret_file_env)
        self.vision_config = config

    async def describe(self, image: bytes, width: int, height: int) -> VisionResult:
        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.vision_config.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": load_prompt("vision/describe"),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                        }
                    ],
                },
            ],
        }
        body.update(self.vision_config.request_options.model_dump(exclude_none=True))
        if self.vision_config.response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        raw = await self.request("/chat/completions", body)
        try:
            choice = raw["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                raise ValueError("incomplete vision result")
            result = VisionResult.model_validate_json(choice["message"]["content"])
            if not result.description.strip() or any(len(s) > 2000 for s in result.uncertainties):
                raise ValueError("invalid vision content")
            return result
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderFailure("VISION_RESPONSE_INVALID") from None
