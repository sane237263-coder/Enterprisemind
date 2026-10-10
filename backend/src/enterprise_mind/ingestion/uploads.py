import asyncio
import hashlib
import queue
import re
from dataclasses import dataclass, field
from io import RawIOBase
from typing import Any, BinaryIO, cast

from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.security import normalize_name
from enterprise_mind.infrastructure.settings import UploadConfig
from enterprise_mind.ingestion.storage import BLOCK_SIZE, ObjectStorage

MIMES = {
    "pdf": "application/pdf",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def matches_signature(extension: str, prefix: bytes) -> bool:
    signatures = {
        "pdf": b"%PDF-",
        "docx": b"PK\x03\x04",
        "pptx": b"PK\x03\x04",
        "xlsx": b"PK\x03\x04",
        "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "ppt": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "png": b"\x89PNG\r\n\x1a\n",
        "jpg": b"\xff\xd8\xff",
        "jpeg": b"\xff\xd8\xff",
    }
    if extension in signatures:
        return prefix.startswith(signatures[extension])
    if extension == "webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    return not (
        prefix.startswith((b"MZ", *signatures.values(), b"RIFF"))
        or (b"\x00" in prefix and not prefix.startswith((b"\xff\xfe", b"\xfe\xff")))
    )


ALLOWED_MIMES = {
    "pdf": {"application/pdf", "application/octet-stream"},
    "md": {"text/markdown", "text/plain", "application/octet-stream"},
    "markdown": {"text/markdown", "text/plain", "application/octet-stream"},
    "txt": {"text/plain", "application/octet-stream"},
    "csv": {"text/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"},
}
for _extension, _mime in MIMES.items():
    ALLOWED_MIMES.setdefault(_extension, {_mime, "application/octet-stream"})


class QueueReader(RawIOBase):
    def __init__(self) -> None:
        self.queue: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=2)
        self.pending = b""
        self.ended = False

    def read(self, size: int = -1) -> bytes:
        size = 5 * 1024 * 1024 if size < 0 else size
        while len(self.pending) < size and not self.ended:
            item = self.queue.get(timeout=30)
            if isinstance(item, Exception):
                raise item
            if item is None:
                self.ended = True
            else:
                self.pending += item
        result, self.pending = self.pending[:size], self.pending[size:]
        return result


@dataclass
class UploadedFile:
    filename: str
    declared_mime: str
    extension: str = ""
    mime_type: str = ""
    size: int = 0
    digest: str = ""
    key: str | None = None
    error: ErrorCode | None = None
    prefix: bytes = b""
    checksum: Any = field(default_factory=hashlib.sha256, repr=False)
    reader: QueueReader | None = field(default=None, repr=False)
    transfer: asyncio.Task[None] | None = field(default=None, repr=False)

    def validate_name(self, config: UploadConfig) -> None:
        if len(self.filename) > 500 or re.search(r"[/\\\x00-\x1f]", self.filename):
            self.error = ErrorCode.VALIDATION_ERROR
            return
        try:
            normalize_name(self.filename, max_length=500)
        except ValueError:
            self.error = ErrorCode.VALIDATION_ERROR
            return
        self.extension = self.filename.rsplit(".", 1)[-1].lower()
        if self.extension not in config.allowed_extensions:
            self.error = ErrorCode.UNSUPPORTED_FILE_TYPE
        elif self.declared_mime not in ALLOWED_MIMES[self.extension]:
            self.error = ErrorCode.FILE_CONTENT_MISMATCH
        else:
            self.mime_type = MIMES[self.extension]

    async def send(self, item: bytes | Exception | None) -> None:
        assert self.reader is not None and self.transfer is not None
        while True:
            if self.transfer.done():
                await self.transfer
                if item is not None:
                    raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
                return
            try:
                await asyncio.to_thread(self.reader.queue.put, item, True, 0.1)
                return
            except queue.Full:
                continue

    async def append(self, data: bytes, storage: ObjectStorage, config: UploadConfig) -> None:
        self.size += len(data)
        self.checksum.update(data)
        if len(self.prefix) < BLOCK_SIZE:
            self.prefix += data[: BLOCK_SIZE - len(self.prefix)]
        if self.size > config.max_bytes:
            self.error = ErrorCode.FILE_TOO_LARGE
        if self.error is not None:
            return
        if self.transfer is None:
            self.reader = QueueReader()
            self.key = storage.new_key()
            self.transfer = asyncio.create_task(
                asyncio.to_thread(
                    storage.upload, self.key, cast(BinaryIO, self.reader), self.mime_type
                )
            )
        try:
            await self.send(data)
        except Exception:
            self.error = ErrorCode.STORAGE_UNAVAILABLE

    async def finish(self, storage: ObjectStorage) -> None:
        self.digest = self.checksum.hexdigest()
        if self.error is None:
            if not self.size or not matches_signature(self.extension, self.prefix):
                self.error = ErrorCode.FILE_CONTENT_MISMATCH
        if self.transfer is not None:
            try:
                await self.send(APIError(self.error) if self.error else None)
                await self.transfer
                if self.error is None:
                    assert self.key is not None
                    await asyncio.to_thread(storage.verify, self.key, self.size, self.digest)
            except Exception:
                self.error = self.error or ErrorCode.STORAGE_UNAVAILABLE


async def receive_upload(
    request: Request,
    storage: ObjectStorage,
    config: UploadConfig,
    *,
    allowed_fields: set[str] | None = None,
) -> tuple[list[UploadedFile], dict[str, str]]:
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    if content_type != b"multipart/form-data" or b"boundary" not in options:
        raise APIError(ErrorCode.INVALID_REQUEST)
    if len(options[b"boundary"]) > 200:
        raise APIError(ErrorCode.INVALID_REQUEST)
    actions: list[tuple[str, Any]] = []
    headers: dict[bytes, bytes] = {}
    header_name, header_value = bytearray(), bytearray()
    header_bytes = 0
    ended = False

    def append_header(target: bytearray, data: bytes, start: int, finish: int) -> None:
        nonlocal header_bytes
        header_bytes += finish - start
        if header_bytes > 16384:
            raise APIError(ErrorCode.INVALID_REQUEST)
        target.extend(data[start:finish])

    def header_end() -> None:
        headers[bytes(header_name).lower()] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_finished() -> None:
        nonlocal header_bytes
        _, disposition = parse_options_header(headers.get(b"content-disposition", b""))
        actions.append(
            (
                "start",
                (dict(disposition), headers.get(b"content-type", b"application/octet-stream")),
            )
        )
        headers.clear()
        header_bytes = 0

    def end() -> None:
        nonlocal ended
        ended = True

    parser = MultipartParser(
        options[b"boundary"],
        {
            "on_header_field": lambda data, start, finish: append_header(
                header_name, data, start, finish
            ),
            "on_header_value": lambda data, start, finish: append_header(
                header_value, data, start, finish
            ),
            "on_header_end": header_end,
            "on_headers_finished": headers_finished,
            "on_part_data": lambda data, start, finish: actions.append(
                ("data", data[start:finish])
            ),
            "on_part_end": lambda: actions.append(("end", None)),
            "on_end": end,
        },
    )
    files: list[UploadedFile] = []
    fields: dict[str, str] = {}
    current: UploadedFile | None = None
    field_name: str | None = None
    field_data = bytearray()
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > config.max_bytes * config.max_files + 1048576:
                raise APIError(ErrorCode.FILE_TOO_LARGE)
            for offset in range(0, len(chunk), BLOCK_SIZE):
                parser.write(chunk[offset : offset + BLOCK_SIZE])
                for action, data in actions:
                    if action == "start":
                        disposition, mime = data
                        name = disposition.get(b"name", b"").decode("utf-8")
                        if b"filename" in disposition:
                            if name != "files" or len(files) >= config.max_files:
                                raise APIError(ErrorCode.VALIDATION_ERROR)
                            current = UploadedFile(
                                disposition[b"filename"].decode("utf-8"),
                                mime.decode("ascii").split(";", 1)[0].lower(),
                            )
                            current.validate_name(config)
                            files.append(current)
                            field_name = None
                        else:
                            if (
                                name
                                not in (
                                    allowed_fields
                                    if allowed_fields is not None
                                    else {"folder_id", "name", "description"}
                                )
                                or name in fields
                            ):
                                raise APIError(ErrorCode.VALIDATION_ERROR)
                            current, field_name = None, name
                            field_data.clear()
                    elif action == "data":
                        if current is not None:
                            await current.append(data, storage, config)
                        else:
                            field_data.extend(data)
                            if len(field_data) > 65536:
                                raise APIError(ErrorCode.VALIDATION_ERROR)
                    elif current is not None:
                        await current.finish(storage)
                        current = None
                    elif field_name is not None:
                        fields[field_name] = field_data.decode("utf-8")
                        field_name = None
                actions.clear()
        parser.finalize()
        if not ended or not files:
            raise APIError(ErrorCode.INVALID_REQUEST)
        return files, fields
    except APIError:
        raise
    except (ValueError, UnicodeError):
        raise APIError(ErrorCode.INVALID_REQUEST) from None
    finally:
        for file in files:
            if file.transfer is not None and not file.transfer.done():
                file.error = file.error or ErrorCode.INVALID_REQUEST
                await file.finish(storage)
