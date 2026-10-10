"""Real isolated converter acceptance; retain only public/synthetic samples and bounded evidence."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pymupdf
from enterprise_mind.infrastructure.settings import OfficeConverterConfig, WorkerConfig
from enterprise_mind.ingestion.office import OfficeConverter
from enterprise_mind.ingestion.parsing import parse_file
from enterprise_mind.ingestion.rich_parsing import render_pdf_page
from PIL import Image, ImageChops


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    version = subprocess.check_output(["libreoffice", "--version"], text=True).strip()
    assert version.startswith("LibreOffice 26.2.6")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/tests"))
    from media_samples import visual_pptx_bytes
    from office_visual_samples import excel_visual_bytes, word_visual_bytes

    args.output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="em-slide-render-") as directory:
        root = Path(directory)
        token = root / "token"
        token.write_text(secrets.token_urlsafe(32))
        os.environ["EM_OFFICE_CONVERTER_TOKEN_FILE"] = str(token)
        samples = list(args.samples.glob("smartart-*.pptx"))
        assert samples, "public SmartArt fixtures required"
        for name, options in (
            ("vector-flow", {}),
            ("xy-chart", {"chart": True}),
            ("hidden-slide", {"hidden": True}),
            ("layout-flow", {"inherited": "layout"}),
            ("master-flow", {"inherited": "master"}),
        ):
            source = root / (name + ".pptx")
            source.write_bytes(visual_pptx_bytes(**options))
            samples.append(source)
        for name, extension, builder in (
            ("word-chart", "docx", lambda: word_visual_bytes()),
            ("word-drawing", "docx", lambda: word_visual_bytes(shape=True)),
            ("word-header-chart", "docx", lambda: word_visual_bytes(header=True)),
            ("excel-chart", "xlsx", excel_visual_bytes),
        ):
            source = root / (name + "." + extension)
            source.write_bytes(builder())
            samples.append(source)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "enterprise_mind.office_converter:factory",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18808",
                "--no-access-log",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get("http://127.0.0.1:18808/").status_code == 404:
                            break
                    except httpx.HTTPError:
                        pass
                    assert process.poll() is None
                    time.sleep(0.1)
            converter = OfficeConverter(
                OfficeConverterConfig(base_url="http://127.0.0.1:18808"), 104857600
            )
            rows = []
            for source in samples:
                extension = source.suffix[1:]
                parsed = parse_file(source, extension, WorkerConfig())
                slides = [s for s in parsed.sources if s.kind == "slide"]
                selected = [s for s in parsed.sources if s.metadata.get("needs_render")]
                assert selected, source.name + " has no detected visual objects"
                pdf_path = args.output / (source.stem + ".pdf")
                unit = selected[0].metadata.get("render_unit")
                asyncio.run(converter.render(source, extension, pdf_path, unit=unit))
                expected = len(slides) if unit is None else 1
                with pymupdf.open(pdf_path) as pdf:
                    assert len(pdf) == expected, "visual-to-page mapping drift"
                    if extension in {"docx", "xlsx"}:
                        text = "".join(page.get_text() for page in pdf)
                        assert (
                            "DRAWING-ONLY"
                            if source.stem == "word-drawing"
                            else "CHART-ONLY"
                        ) in text, source.name + " lost native visual text"
                images = []
                for slide in selected:
                    output = args.output / source.stem / str(slide.ordinal + 1)
                    rendered = render_pdf_page(
                        pdf_path,
                        slide.ordinal if unit is None else 0,
                        WorkerConfig(),
                        output,
                        expected_pages=expected,
                    )
                    image = output / rendered.artifacts[0].file_name
                    with Image.open(image) as pixels:
                        assert ImageChops.difference(
                            pixels.convert("RGB"),
                            Image.new("RGB", pixels.size, "white"),
                        ).getbbox()
                    images.append(str(image.relative_to(args.output)))
                rows.append(
                    {
                        "sample": source.name,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "pages": expected,
                        "visual_slides": len(selected),
                        "render": "passed",
                        "images": images,
                    }
                )
            (args.output / "report.json").write_text(
                json.dumps(
                    {"converter": version, "network": "none", "samples": rows},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"Real Office visual rendering passed: {len(rows)} samples.")
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
