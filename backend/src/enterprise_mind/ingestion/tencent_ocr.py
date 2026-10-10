"""TC3 OCR wire protocol. Actions/endpoints come from configuration, not domain code."""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from enterprise_mind.infrastructure.model_transport import model_proxy_url
from enterprise_mind.infrastructure.settings import ConfigurationError, read_secret
from enterprise_mind.ingestion.media_providers import Box, OCRBlock, OCRResult, OCRTable
from enterprise_mind.rag.config import TencentOCRConfig
from enterprise_mind.rag.providers import ProviderFailure


def tc3_headers(
    config: TencentOCRConfig,
    secret_id: str,
    secret_key: str,
    action: str,
    payload: bytes,
    timestamp: int,
) -> dict[str, str]:
    host = urlsplit(config.base_url).netloc
    content_type = "application/json; charset=utf-8"
    signed_headers = "content-type;host;x-tc-action"
    canonical_headers = (
        f"content-type:{content_type}\nhost:{host.lower()}\nx-tc-action:{action.lower()}\n"
    )
    canonical = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, hashlib.sha256(payload).hexdigest()]
    )
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    scope = f"{date}/ocr/tc3_request"
    to_sign = "\n".join(
        ["TC3-HMAC-SHA256", str(timestamp), scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = ("TC3" + secret_key).encode()
    for part in (date, "ocr", "tc3_request"):
        key = hmac.digest(key, part.encode(), "sha256")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}",
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": config.api_version,
        "X-TC-Timestamp": str(timestamp),
    }
    if config.region:
        headers["X-TC-Region"] = config.region
    return headers


def _number(value: object) -> float:
    if not isinstance(value, float | int) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("invalid coordinate")
    return float(value)


def _box(points: Any, width: int, height: int) -> Box:
    if not isinstance(points, list) or len(points) != 4:
        raise ValueError("invalid polygon")
    xs, ys = [_number(p["X"]) for p in points], [_number(p["Y"]) for p in points]
    result = Box(x=min(xs), y=min(ys), width=max(xs) - min(xs), height=max(ys) - min(ys))
    if max(xs) > width or max(ys) > height:
        raise ValueError("polygon outside image")
    return result


def _column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def parse_text(raw: dict[str, Any], width: int, height: int) -> list[OCRBlock]:
    detections = raw["TextDetections"]
    if not isinstance(detections, list) or len(detections) > 10000:
        raise ValueError("invalid detections")
    blocks = []
    for item in detections:
        text = item["DetectedText"]
        if not isinstance(text, str):
            raise ValueError("invalid text")
        if not text.strip():
            continue
        points = item.get("Polygon")
        if not points:
            rectangle = item["ItemPolygon"]
            x, y = _number(rectangle["X"]), _number(rectangle["Y"])
            w, h = _number(rectangle["Width"]), _number(rectangle["Height"])
            if w <= 0 or h <= 0:
                raise ValueError("empty rectangle")
            points = [
                dict(X=x, Y=y),
                dict(X=x + w, Y=y),
                dict(X=x + w, Y=y + h),
                dict(X=x, Y=y + h),
            ]
        confidence = item.get("Confidence")
        blocks.append(
            OCRBlock(
                text=text,
                box=_box(points, width, height),
                confidence=_number(confidence) / 100 if confidence is not None else None,
            )
        )
    return blocks


def parse_tables(raw: dict[str, Any], width: int, height: int) -> list[OCRTable]:
    detections = raw["TableDetections"]
    if detections is None:
        return []
    if not isinstance(detections, list) or len(detections) > 1000:
        raise ValueError("invalid tables")
    tables = []
    for table in detections:
        if table.get("Type") == 0:
            continue  # Non-table text is already covered by the text Action.
        cells = table["Cells"]
        if cells is None or cells == []:
            continue
        if not isinstance(cells, list) or len(cells) > 10000:
            raise ValueError("invalid cells")
        for cell in cells:
            indices = [cell[name] for name in ("RowTl", "RowBr", "ColTl", "ColBr")]
            if any(type(value) is not int for value in indices):
                raise ValueError("invalid cell index")
            if not (0 <= indices[0] < indices[1] <= 10000 and 0 <= indices[2] < indices[3] <= 1000):
                raise ValueError("invalid cell span")
            _box(cell["Polygon"], width, height)
        row_count, col_count = max(c["RowBr"] for c in cells), max(c["ColBr"] for c in cells)
        if row_count * col_count > 100000:
            raise ValueError("table too large")
        rows = [[""] * col_count for _ in range(row_count)]
        occupied: set[tuple[int, int]] = set()
        merged = []
        for cell in cells:
            top, bottom, left, right = (cell[k] for k in ("RowTl", "RowBr", "ColTl", "ColBr"))
            if not isinstance(cell["Text"], str) or len(cell["Text"]) > 100000:
                raise ValueError("invalid cell text")
            for r in range(top, bottom):
                for c in range(left, right):
                    if (r, c) in occupied:
                        raise ValueError("overlapping cells")
                    occupied.add((r, c))
            rows[top][left] = cell["Text"]
            if bottom - top > 1 or right - left > 1:
                merged.append(f"{_column(left + 1)}{top + 1}:{_column(right)}{bottom}")
        points = table.get("TableCoordPoint")
        if not points:
            points = [point for cell in cells for point in cell["Polygon"]]
            xs, ys = [_number(p["X"]) for p in points], [_number(p["Y"]) for p in points]
            points = [
                dict(X=min(xs), Y=min(ys)),
                dict(X=max(xs), Y=min(ys)),
                dict(X=max(xs), Y=max(ys)),
                dict(X=min(xs), Y=max(ys)),
            ]
        tables.append(OCRTable(rows=rows, box=_box(points, width, height), merged_ranges=merged))
    return tables


class TencentOCR:
    def __init__(self, config: TencentOCRConfig) -> None:
        self.config, self.model_key = config, config.model_key
        id_file, key_file = (
            os.environ.get(config.secret_id_file_env),
            os.environ.get(config.secret_key_file_env),
        )
        if not id_file or not key_file:
            raise ConfigurationError("已启用的 OCR 缺少 SecretId/SecretKey 文件配置。")
        self._id, self._key = read_secret(Path(id_file)), read_secret(Path(key_file))
        self.proxy = model_proxy_url()

    async def request(self, action: str, image: bytes) -> dict[str, Any]:
        encoded = base64.b64encode(image)
        if not image or len(encoded) > self.config.max_image_base64_bytes:
            raise ProviderFailure("OCR_INPUT_INVALID")
        payload = json.dumps(
            {"ImageBase64": encoded.decode("ascii")}, separators=(",", ":")
        ).encode()
        for attempt in range(self.config.max_retries + 1):
            headers = tc3_headers(
                self.config,
                self._id.get_secret_value(),
                self._key.get_secret_value(),
                action,
                payload,
                int(time.time()),
            )
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
                        self.config.base_url.rstrip("/") + "/",
                        headers=headers,
                        content=payload,
                    ) as response,
                ):
                    if response.status_code in {408, 429, 500, 502, 503, 504}:
                        raise httpx.ReadTimeout("retry")
                    if response.status_code != 200:
                        raise ProviderFailure("OCR_PROVIDER_REJECTED")
                    content = bytearray()
                    async for part in response.aiter_bytes():
                        content.extend(part)
                        if len(content) > 4 * 1024 * 1024:
                            raise ProviderFailure("OCR_RESPONSE_INVALID")
                    raw = json.loads(content)["Response"]
                    if not isinstance(raw, dict):
                        raise ValueError("invalid response")
                    if "Error" not in raw:
                        return raw
                    code = raw["Error"]["Code"]
                    if not isinstance(code, str):
                        raise ValueError("invalid error")
                    if code.startswith(("InternalError", "RequestLimitExceeded")):
                        raise httpx.ReadTimeout("retry")
                    if code.startswith("AuthFailure"):
                        raise ProviderFailure("OCR_AUTH_FAILED")
                    if code.startswith("UnauthorizedOperation"):
                        raise ProviderFailure("OCR_PERMISSION_DENIED")
                    raise ProviderFailure("OCR_PROVIDER_REJECTED")
            except (httpx.TimeoutException, httpx.NetworkError, TimeoutError):
                if attempt < self.config.max_retries:
                    await asyncio.sleep(min(2.0, 0.25 * 2**attempt))
            except (ValueError, KeyError, TypeError, httpx.HTTPError):
                raise ProviderFailure("OCR_RESPONSE_INVALID") from None
        raise ProviderFailure("OCR_PROVIDER_UNAVAILABLE")

    async def recognize(self, image: bytes, width: int, height: int) -> OCRResult:
        if width <= 0 or height <= 0:
            raise ProviderFailure("OCR_INPUT_INVALID")
        text = await self.request(self.config.model, image)
        try:
            blocks = parse_text(text, width, height)
            tables = (
                await self.request(self.config.table_action, image)
                if self.config.table_action
                else None
            )
            return OCRResult(
                blocks=blocks,
                tables=parse_tables(tables, width, height) if tables is not None else [],
            )
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ProviderFailure("OCR_RESPONSE_INVALID") from None
