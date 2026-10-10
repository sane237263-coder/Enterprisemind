"""Bounded child process: no service credentials, SQL connection, or network clients."""

import importlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        if os.name == "posix":
            resource: Any = importlib.import_module("resource")

            limit = request["config"]["parse_memory_limit_mb"] * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        from enterprise_mind.infrastructure.settings import WorkerConfig
        from enterprise_mind.ingestion.parsing import parse_file

        with redirect_stdout(io.StringIO()):
            if request.get("render_validation"):
                from enterprise_mind.ingestion.rich_parsing import check_package

                check_package(
                    Path(request["path"]),
                    request["extension"],
                    WorkerConfig.model_validate(request["config"]),
                    forbid_external_relationships=True,
                )
                if request.get("render_unit") is not None:
                    from enterprise_mind.ingestion.office_visuals import render_copy

                    render_copy(
                        Path(request["path"]),
                        request["extension"],
                        request["render_unit"],
                        Path(request["output"]).parent / ("render-source." + request["extension"]),
                    )
                elif request["extension"] == "pptx":
                    from enterprise_mind.ingestion.smartart_render import smartart_render_copy

                    smartart_render_copy(
                        Path(request["path"]),
                        Path(request["output"]).parent / "render-source.pptx",
                    )
                from enterprise_mind.ingestion.parsing import ParsedDocument

                result = ParsedDocument(sources=[])
            elif request["extension"] == "preview_snapshot":
                from enterprise_mind.ingestion.parsing import ParsedDocument
                from enterprise_mind.ingestion.rich_parsing import finish_artifacts

                config = WorkerConfig.model_validate(request["config"])
                source = Path(request["path"])
                if source.stat().st_size > config.max_text_chars * 12 + config.max_elements * 4096:
                    raise ValueError("preview snapshot limit")
                result = ParsedDocument.model_validate_json(source.read_text(encoding="utf-8"))
                finish_artifacts(
                    result, source, "preview_snapshot", config, Path(request["artifacts"])
                )
            elif request.get("page") is not None:
                from enterprise_mind.ingestion.rich_parsing import render_pdf_page

                result = render_pdf_page(
                    Path(request["path"]),
                    request["page"],
                    WorkerConfig.model_validate(request["config"]),
                    Path(request["artifacts"]),
                    expected_pages=request.get("expected_pages"),
                )
            else:
                result = parse_file(
                    Path(request["path"]),
                    request["extension"],
                    WorkerConfig.model_validate(request["config"]),
                    Path(request["artifacts"]) if request.get("artifacts") else None,
                )
        Path(request["output"]).write_text(result.model_dump_json(), encoding="utf-8")
        sys.stdout.write("ok")
    except Exception:
        sys.stdout.write("PARSE_FAILED")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
