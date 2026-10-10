"""Internal conversion client; filenames and targets never come from model arguments."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from enterprise_mind.infrastructure.settings import (
    ConfigurationError,
    OfficeConverterConfig,
    read_secret,
)
from enterprise_mind.rag.providers import ProviderFailure

LEGACY_FORMATS = {"doc": "docx", "ppt": "pptx", "xls": "xlsx"}


class OfficeConverter:
    def __init__(self, config: OfficeConverterConfig, max_bytes: int) -> None:
        self.config, self.max_bytes = config, max_bytes
        secret = os.environ.get("EM_OFFICE_CONVERTER_TOKEN_FILE")
        if not secret:
            raise ConfigurationError("Office 转换服务缺少 Secret 文件配置。")
        self._token = read_secret(Path(secret))

    async def convert(self, source: Path, extension: str, destination: Path) -> str:
        if extension not in LEGACY_FORMATS:
            raise ValueError("unsupported conversion")
        await self._request(source, extension, destination, "convert", b"PK\x03\x04")
        return LEGACY_FORMATS[extension]

    async def render(
        self, source: Path, extension: str, destination: Path, *, unit: int | None = None
    ) -> None:
        if extension not in {"ppt", "pptx", "docx", "xlsx"}:
            raise ValueError("unsupported rendering")
        await self._request(source, extension, destination, "render", b"%PDF-", unit=unit)

    async def _request(
        self,
        source: Path,
        extension: str,
        destination: Path,
        operation: str,
        signature: bytes,
        *,
        unit: int | None = None,
    ) -> None:

        async def content() -> AsyncIterator[bytes]:
            with source.open("rb") as handle:
                while part := await asyncio.to_thread(handle.read, 65536):
                    yield part

        try:
            async with (
                asyncio.timeout(self.config.timeout_seconds),
                httpx.AsyncClient(
                    timeout=self.config.timeout_seconds, trust_env=False, follow_redirects=False
                ) as client,
                client.stream(
                    "POST",
                    self.config.base_url.rstrip("/") + "/" + operation + "/" + extension,
                    content=content(),
                    params={"unit": unit} if unit is not None else None,
                    headers={
                        "Authorization": "Bearer " + self._token.get_secret_value(),
                        "Content-Type": "application/octet-stream",
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise ProviderFailure("OFFICE_CONVERSION_FAILED")
                total = 0
                with destination.open("wb") as output:
                    async for part in response.aiter_bytes():
                        total += len(part)
                        if total > self.max_bytes:
                            raise ProviderFailure("OFFICE_CONVERSION_FAILED")
                        output.write(part)
                with destination.open("rb") as converted:
                    if not converted.read(len(signature)).startswith(signature):
                        raise ProviderFailure("OFFICE_CONVERSION_FAILED")
        except (httpx.HTTPError, TimeoutError, OSError):
            raise ProviderFailure("OFFICE_CONVERSION_FAILED") from None
