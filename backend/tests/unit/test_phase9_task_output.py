import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.generation.rendering import RenderedFile
from enterprise_mind.generation.service import task_output
from enterprise_mind.generation.worker import GenerationWorker


def make_task(
    status: str,
    *,
    phase: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> GenerationTask:
    return GenerationTask(
        id=11,
        kind="content_render",
        format="md",
        scope=None,
        status=status,
        phase=phase,
        processed_rows=0,
        total_rows=None,
        exported_rows=None,
        error_code=error_code,
        error_message=error_message,
    )


def make_file() -> GeneratedFile:
    return GeneratedFile(
        id=22,
        file_name="report.md",
        size_bytes=123,
        source_refs=[{"source_type": "knowledge", "document_id": "7"}],
        generated_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
    )


def test_task_output_requires_a_real_file_for_completion() -> None:
    succeeded = task_output(make_task("succeeded", phase="published"), make_file())
    assert succeeded["complete"] is True
    assert succeeded["file_id"] == "22"
    assert succeeded["file_name"] == "report.md"
    assert succeeded["size_bytes"] == 123
    assert succeeded["source_refs"] == [{"source_type": "knowledge", "document_id": "7"}]
    assert succeeded["generated_at"] == "2026-09-20T10:00:00+00:00"

    missing = task_output(make_task("succeeded", phase="published"))
    assert missing["status"] == "succeeded"
    assert missing["phase"] == "published"
    assert missing["complete"] is False
    assert missing["file_id"] is None
    assert "file_name" not in missing


def test_chart_task_output_exposes_one_frozen_four_format_bundle() -> None:
    task = GenerationTask(
        id=31,
        kind="chart_render",
        format="pdf",
        scope="all_matching",
        status="succeeded",
        phase="published",
        input_ref={
            "chart": {
                "chart_type": "bar",
                "title": "部门金额",
                "scope": "all_aggregated",
                "statistic": "SUM(amount)；按 department 分组",
            }
        },
    )
    files = [
        GeneratedFile(
            id=index,
            file_format=file_format,
            file_name=f"chart.{file_format}",
            size_bytes=index * 10,
            source_refs=[{"source_type": "database", "executed_at": "2026-09-22T08:00:00+00:00"}],
            generated_at=datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
        )
        for index, file_format in enumerate(("png", "pdf", "xlsx", "csv"), 1)
    ]

    output = task_output(task, files=files)

    assert output["complete"] is True
    assert output["file_id"] == "2"
    assert [artifact["format"] for artifact in output["artifacts"]] == [
        "csv",
        "pdf",
        "png",
        "xlsx",
    ]
    assert output["preview_url"] == "/api/v1/generated-files/1/preview"
    assert output["artifacts"][0]["download_url"].endswith("/4/download")


@pytest.mark.parametrize(
    ("status", "phase", "error_code", "error_message"),
    [
        ("failed", "failed", "FILE_GENERATION_TIMEOUT", "生成文件超过时间预算。"),
        ("cancelled", "cancelled", "CANCELLED", "任务已取消。"),
    ],
)
def test_task_output_keeps_terminal_error_separate_from_file(
    status: str, phase: str, error_code: str, error_message: str
) -> None:
    output = task_output(
        make_task(status, phase=phase, error_code=error_code, error_message=error_message),
        make_file(),
    )

    assert output["status"] == status
    assert output["phase"] == phase
    assert output["complete"] is False
    assert output["file_id"] is None
    assert output["error"] == {"code": error_code, "message": error_message}
    assert "file_name" not in output


def _worker_without_cancellation() -> GenerationWorker:
    worker = object.__new__(GenerationWorker)

    async def not_cancelled(_task_id: int) -> bool:
        return False

    worker._cancelled = not_cancelled
    return worker


def _page_plan(columns: tuple[dict[str, object], ...]) -> SimpleNamespace:
    return SimpleNamespace(
        scope="current_page",
        page=SimpleNamespace(
            columns=columns,
            rows=({"id": "001"},),
            queried_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        ),
        guarded_query=SimpleNamespace(referenced_relations=()),
    )


def test_export_frames_rejects_invalid_column_metadata_before_indexing() -> None:
    worker = _worker_without_cancellation()

    async def collect() -> list[dict[str, object]]:
        return [frame async for frame in worker._export_frames(_page_plan(({"name": None},)), 7)]

    with pytest.raises(APIError) as error:
        asyncio.run(collect())
    assert error.value.code == ErrorCode.FILE_CONTENT_MISMATCH


def test_export_frames_uses_narrowed_valid_column_names() -> None:
    worker = _worker_without_cancellation()

    async def collect() -> list[dict[str, object]]:
        return [frame async for frame in worker._export_frames(_page_plan(({"name": "id"},)), 7)]

    frames = asyncio.run(collect())
    assert frames[0]["type"] == "header"
    assert frames[1] == {"type": "batch", "sequence": 1, "rows": [["001"]]}
    assert frames[2]["complete"] is True


def test_chart_worker_binds_four_formats_to_one_streamed_snapshot(tmp_path) -> None:
    worker = _worker_without_cancellation()

    async def progress(_task_id: int, _rows: int) -> None:
        return None

    worker._set_progress = progress
    task = GenerationTask(
        id=8,
        owner_user_id=11,
        conversation_id=22,
        kind="chart_render",
        format="png",
        scope="all_aggregated",
        input_ref={
            "query_id": "query-8",
            "result_id": None,
            "formats": ["png", "pdf", "xlsx", "csv"],
            "chart": {
                "chart_type": "bar",
                "title": "部门金额",
                "scope": "all_aggregated",
                "dimension_column": "department",
                "metric_column": "amount",
                "statistic": "SUM(amount)；按 department 分组",
                "dimension_kind": "category",
                "sort_order": "input",
                "top_n": None,
            },
        },
        source_refs=[{"source_type": "database", "query_kind": "aggregate"}],
    )
    plan = SimpleNamespace(
        database_source_id=1,
        schema_snapshot_id=2,
        source_version=3,
        permission_fingerprint="permission-fingerprint",
        referenced_relations=('"public"."orders"',),
    )

    async def frames():
        yield {
            "type": "header",
            "columns": [
                {"name": "department", "data_type": "text"},
                {"name": "amount", "data_type": "numeric"},
            ],
            "referenced_relations": ['"public"."orders"'],
        }
        yield {"type": "batch", "sequence": 1, "rows": [["研发", "12.50"], ["销售", "8.25"]]}
        yield {
            "type": "end",
            "exported_rows": 2,
            "complete": True,
            "executed_at": "2026-09-22T08:00:00+00:00",
        }

    rendered = asyncio.run(
        worker._render_chart(
            task,
            task.input_ref,
            tmp_path,
            frames(),
            plan,
            task.source_refs,
        )
    )
    assert rendered.file_format == "png"
    assert [artifact.file_format for artifact in rendered.artifacts] == [
        "png",
        "pdf",
        "xlsx",
        "csv",
    ]
    assert all(
        artifact.executed_at == "2026-09-22T08:00:00+00:00" for artifact in rendered.artifacts
    )
    assert {path.suffix for path in tmp_path.iterdir()} == {".png", ".pdf", ".xlsx", ".csv"}
    assert all(path.stat().st_size > 0 for path in tmp_path.iterdir())


def test_chart_bundle_upload_failure_removes_every_partial_object(tmp_path) -> None:
    class FailingStorage:
        bucket = "test-bucket"

        def __init__(self) -> None:
            self.calls = 0
            self.removed: list[str] = []

        @staticmethod
        def new_key(prefix: str) -> str:
            return f"{prefix}/artifact-{id(prefix)}"

        def upload(self, key: str, stream, mime: str) -> None:
            del key, stream, mime
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated object storage failure")

        @staticmethod
        def verify(key: str, size: int, digest: str) -> None:
            del key, size, digest

        def remove(self, key: str) -> None:
            self.removed.append(key)

    first = tmp_path / "chart.png"
    second = tmp_path / "chart.pdf"
    first.write_bytes(b"png")
    second.write_bytes(b"pdf")
    rendered = (
        RenderedFile(first, "png", "image/png", 3, "a" * 64),
        RenderedFile(second, "pdf", "application/pdf", 3, "b" * 64),
    )
    storage = FailingStorage()
    worker = object.__new__(GenerationWorker)
    worker.storage = storage

    with pytest.raises(RuntimeError, match="simulated object storage failure"):
        asyncio.run(worker._upload_rendered_files(rendered, export=True))

    assert len(storage.removed) == 2
