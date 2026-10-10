"""Run untrusted document decoding in a credential-free, bounded subprocess."""

import asyncio
import json
import os
import sys
from pathlib import Path

from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.parsing import ParsedDocument


async def run_parser(
    source: Path,
    extension: str,
    config: WorkerConfig,
    directory: Path,
    *,
    page: int | None = None,
    expected_pages: int | None = None,
    render_validation: bool = False,
    render_unit: int | None = None,
) -> ParsedDocument:
    output = directory / "parsed.json"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}
    }
    environment["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "enterprise_mind.ingestion.parser_process",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
    )
    try:
        body = json.dumps(
            {
                "path": str(source),
                "output": str(output),
                "extension": extension,
                "artifacts": str(directory / "artifacts"),
                "config": config.model_dump(),
                "page": page,
                "expected_pages": expected_pages,
                "render_validation": render_validation,
                "render_unit": render_unit,
            }
        ).encode()
        result, _ = await asyncio.wait_for(process.communicate(body), config.parse_timeout_seconds)
        if process.returncode != 0 or result != b"ok":
            raise ValueError("PARSE_FAILED")
        if output.stat().st_size > config.max_text_chars * 12 + config.max_elements * 4096:
            raise ValueError("PARSE_FAILED")
        return ParsedDocument.model_validate_json(
            await asyncio.to_thread(output.read_text, encoding="utf-8")
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
