"""Local thread lifecycle contracts for cancellation of generation work.

The renderer test exercises the real Markdown renderer.  The upload test uses
a controlled storage substitute to model a buffered upload that can finish
writing after its caller is cancelled; it is unit evidence, not a real MinIO
failure test.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO

import pytest

from enterprise_mind.generation.rendering import (
    RenderedFile,
    RenderInput,
    await_thread_completion,
    render_to_path,
)
from enterprise_mind.generation.worker import GenerationWorker


def test_repeated_cancellation_keeps_render_directory_until_renderer_finishes(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    thread_finished = threading.Event()
    directories: list[Path] = []
    rendered_files: list[RenderedFile] = []

    def blocked_real_renderer(*args: Any, **kwargs: Any) -> RenderedFile:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("test did not release the renderer")
        try:
            result = render_to_path(*args, **kwargs)
            rendered_files.append(result)
            return result
        finally:
            thread_finished.set()

    async def run() -> None:
        async def render_in_owned_directory() -> RenderedFile:
            with TemporaryDirectory(dir=tmp_path, prefix="generation-render-") as raw_directory:
                directory = Path(raw_directory)
                directories.append(directory)
                return await await_thread_completion(
                    blocked_real_renderer,
                    RenderInput("Lifecycle", "# Rendered after release"),
                    "md",
                    directory,
                    "lifecycle.md",
                )

        task = asyncio.create_task(render_in_owned_directory())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert len(directories) == 1 and directories[0].is_dir()

            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not thread_finished.is_set()
            assert directories[0].is_dir()
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await task

        assert task.cancelled()
        assert thread_finished.is_set()
        assert len(rendered_files) == 1
        assert rendered_files[0].size_bytes > 0
        assert len(rendered_files[0].content_sha256) == 64
        assert not directories[0].exists()

    asyncio.run(run())


def test_cancelled_renderer_failure_preserves_cancellation(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    directories: list[Path] = []

    def failing_renderer() -> None:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("test did not release the renderer")
        assert directories[0].is_dir()
        raise ValueError("controlled renderer failure after cancellation")

    async def run() -> None:
        async def render_in_owned_directory() -> None:
            with TemporaryDirectory(dir=tmp_path, prefix="generation-failure-") as raw:
                directories.append(Path(raw))
                await await_thread_completion(failing_renderer)

        task = asyncio.create_task(render_in_owned_directory())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert not directories[0].exists()

    asyncio.run(run())


class _BufferedUploadStorage:
    """A local stub that can complete an upload after its input stream closes."""

    bucket = "unit-only"

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.entered = threading.Event()
        self.upload_finished = threading.Event()
        self.key = "generated/unit-thread-attempt"
        self.stream: BinaryIO | None = None
        self.stream_closed_during_upload: bool | None = None
        self.objects: dict[str, bytes] = {}
        self.remove_before_upload_finished: list[bool] = []

    def new_key(self, prefix: str = "original") -> str:
        assert prefix == "generated"
        return self.key

    def upload(self, key: str, stream: BinaryIO, mime: str) -> None:
        assert mime == "text/markdown"
        self.stream = stream
        buffered_payload = stream.read()
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release the upload")
        self.stream_closed_during_upload = stream.closed
        self.objects[key] = buffered_payload
        self.upload_finished.set()

    def verify(self, key: str, size: int, digest: str) -> None:
        assert key in self.objects
        assert len(self.objects[key]) == size
        assert hashlib.sha256(self.objects[key]).hexdigest() == digest

    def remove(self, key: str) -> None:
        self.remove_before_upload_finished.append(not self.upload_finished.is_set())
        self.objects.pop(key, None)


def test_repeated_cancellation_keeps_upload_stream_open_and_removes_after_thread_finishes(
    tmp_path: Path,
) -> None:
    payload = b"# buffered upload lifecycle\n"
    rendered_path = tmp_path / "buffered.md"
    rendered_path.write_bytes(payload)
    rendered = RenderedFile(
        path=rendered_path,
        file_format="md",
        mime_type="text/markdown",
        size_bytes=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
    )
    release = threading.Event()
    storage = _BufferedUploadStorage(release)
    worker = GenerationWorker(sessions=None, storage=storage, config=None)

    async def run() -> None:
        task = asyncio.create_task(worker._upload_rendered_files((rendered,), export=False))
        try:
            assert await asyncio.to_thread(storage.entered.wait, 5)
            assert storage.stream is not None and not storage.stream.closed

            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not storage.stream.closed
            assert storage.remove_before_upload_finished == []
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await task

        assert task.cancelled()
        assert storage.upload_finished.is_set()
        assert storage.stream is not None and storage.stream.closed
        assert storage.stream_closed_during_upload is False
        assert storage.remove_before_upload_finished == [False]
        assert storage.objects == {}

    asyncio.run(run())
