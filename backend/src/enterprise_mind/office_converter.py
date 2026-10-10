"""Standalone internal LibreOffice service. No application DB/storage/model credentials."""

import asyncio
import hmac
import os
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from enterprise_mind.infrastructure.settings import read_secret
from enterprise_mind.ingestion.office import LEGACY_FORMATS
from enterprise_mind.ingestion.uploads import MIMES, matches_signature

FILTERS = {
    "doc": "docx:Office Open XML Text",
    "ppt": "pptx:Impress MS PowerPoint 2007 XML",
    "xls": "xlsx:Calc MS Excel 2007 XML",
}
RENDER_FILTER = (
    'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"},'
    '"ExportNotesPages":{"type":"boolean","value":"false"}}'
)
PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Save/Document">
<prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>"""
READINESS_TIMEOUT_SECONDS = 5


async def converter_ready(executable: str) -> bool:
    """Check the engine can start and create its private temporary workspace."""
    try:
        with TemporaryDirectory(prefix="em-office-ready-") as work:
            root = Path(work)
            (root / "write-probe").write_bytes(b"ready")
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LD_LIBRARY_PATH"}
            }
            environment.update(HOME=str(root), TMPDIR=str(root), SAL_USE_VCLPLUGIN="svp")
            process = await asyncio.create_subprocess_exec(
                executable,
                "-env:UserInstallation=" + (root / "profile").as_uri(),
                "--headless",
                "--version",
                cwd=root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), READINESS_TIMEOUT_SECONDS)
                return process.returncode == 0 and output.startswith(b"LibreOffice ")
            finally:
                if sys.platform != "win32":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.returncode is None:
                    process.kill()
                await process.wait()
    except (OSError, TimeoutError):
        return False


def create_converter(
    token_file: Path,
    executable: str = "/usr/bin/libreoffice",
    *,
    timeout_seconds: int = 180,
    max_bytes: int = 104857600,
) -> FastAPI:
    token = read_secret(token_file)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(1)
    readiness_slot = asyncio.Lock()

    @app.get("/health/live")
    async def live() -> Response:
        return Response("ok", headers={"Cache-Control": "no-store"})

    @app.get("/health/ready")
    async def ready() -> Response:
        async with readiness_slot:
            healthy = await converter_ready(executable)
        return Response(
            "ready" if healthy else "not ready",
            status_code=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/{operation}/{extension}")
    async def convert(
        operation: str, extension: str, request: Request, unit: int | None = None
    ) -> Response:
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            authorization.encode(), ("Bearer " + token.get_secret_value()).encode()
        ):
            raise HTTPException(401, "AUTH_REQUIRED")
        rendering = operation == "render"
        if operation not in {"convert", "render"} or extension not in (
            {"ppt", "pptx", "docx", "xlsx"} if rendering else LEGACY_FORMATS
        ):
            raise HTTPException(422, "UNSUPPORTED_FILE_TYPE")
        if (extension in {"docx", "xlsx"}) != (unit is not None) or (
            unit is not None and not 0 <= unit < 100000
        ):
            raise HTTPException(422, "INVALID_RENDER_UNIT")
        if slots.locked():
            raise HTTPException(429, "CONVERTER_BUSY")
        async with slots, asyncio.timeout(timeout_seconds + 10):
            with TemporaryDirectory(prefix="em-office-") as work:
                root = Path(work)
                source = root / ("source." + extension)
                profile = root / "profile"
                (profile / "user").mkdir(parents=True)
                (profile / "user/registrymodifications.xcu").write_text(PROFILE, encoding="utf-8")
                output = root / "output"
                output.mkdir()
                size, prefix = 0, b""
                with source.open("wb") as handle:
                    async for part in request.stream():
                        size += len(part)
                        prefix = (prefix + part)[:16]
                        if size > max_bytes:
                            raise HTTPException(413, "FILE_TOO_LARGE")
                        handle.write(part)
                if not size or not matches_signature(extension, prefix):
                    raise HTTPException(422, "FILE_CONTENT_MISMATCH")
                if extension in {"pptx", "docx", "xlsx"}:
                    # Validate inside the same credential-free bounded parser used by ingestion.
                    from enterprise_mind.infrastructure.settings import WorkerConfig
                    from enterprise_mind.ingestion.process import run_parser

                    try:
                        await run_parser(
                            source,
                            extension,
                            WorkerConfig(),
                            root,
                            render_validation=True,
                            render_unit=unit,
                        )
                        render_source = root / ("render-source." + extension)
                        if unit is not None or (extension == "pptx" and render_source.is_file()):
                            source = render_source
                    except (ValueError, TimeoutError):
                        raise HTTPException(422, "OFFICE_CONVERSION_FAILED") from None
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LD_LIBRARY_PATH"}
                }
                environment.update(HOME=str(root), TMPDIR=str(root), SAL_USE_VCLPLUGIN="svp")
                process = await asyncio.create_subprocess_exec(
                    executable,
                    "-env:UserInstallation=" + profile.as_uri(),
                    "--headless",
                    "--norestore",
                    "--nodefault",
                    "--nolockcheck",
                    "--convert-to",
                    (
                        "pdf:writer_pdf_Export"
                        if extension == "docx"
                        else "pdf:calc_pdf_Export"
                        if extension == "xlsx"
                        else RENDER_FILTER
                    )
                    if rendering
                    else FILTERS[extension],
                    "--outdir",
                    str(output),
                    str(source),
                    cwd=root,
                    env=environment,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout_seconds)
                    converted = output / (
                        source.stem + "." + ("pdf" if rendering else LEGACY_FORMATS[extension])
                    )
                    if (
                        process.returncode
                        or not converted.is_file()
                        or converted.stat().st_size > max_bytes
                    ):
                        raise HTTPException(422, "OFFICE_CONVERSION_FAILED")
                    data = await asyncio.to_thread(converted.read_bytes)
                    if not data.startswith(b"%PDF-" if rendering else b"PK\x03\x04"):
                        raise HTTPException(422, "OFFICE_CONVERSION_FAILED")
                    return Response(
                        data,
                        media_type="application/pdf"
                        if rendering
                        else MIMES[LEGACY_FORMATS[extension]],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                    )
                except TimeoutError:
                    raise HTTPException(504, "OFFICE_CONVERSION_TIMEOUT") from None
                finally:
                    if sys.platform != "win32":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif process.returncode is None:
                        process.kill()
                    await process.wait()

    return app


def factory() -> FastAPI:
    return create_converter(Path(os.environ["EM_OFFICE_CONVERTER_TOKEN_FILE"]))
