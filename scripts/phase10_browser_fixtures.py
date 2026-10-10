"""Create small, structurally valid files for P10 browser ingestion paths.

The files are local acceptance fixtures only.  They carry no credentials or
enterprise data.  DOCX/XLSX are real OOXML ZIP packages, PDF is written by
PyMuPDF from the locked backend environment, and PNG is a valid image stream.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def write_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value.encode("utf-8"))


def docx(path: Path, label: str) -> None:
    text = escape(f"P10 browser DOCX fixture: {label}")
    write_zip(
        path,
        {
            "[Content_Types].xml": "<?xml version='1.0' encoding='UTF-8'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>",
            "_rels/.rels": "<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>",
            "word/document.xml": f"<?xml version='1.0' encoding='UTF-8' standalone='yes'?><w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body></w:document>",
        },
    )


def xlsx(path: Path, label: str) -> None:
    text = escape(f"P10 browser XLSX fixture: {label}")
    write_zip(
        path,
        {
            "[Content_Types].xml": "<?xml version='1.0' encoding='UTF-8'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/xl/workbook.xml' ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'/><Override PartName='/xl/worksheets/sheet1.xml' ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'/></Types>",
            "_rels/.rels": "<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='xl/workbook.xml'/></Relationships>",
            "xl/_rels/workbook.xml.rels": "<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' Target='worksheets/sheet1.xml'/></Relationships>",
            "xl/workbook.xml": "<?xml version='1.0' encoding='UTF-8'?><workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'><sheets><sheet name='Evidence' sheetId='1' r:id='rId1'/></sheets></workbook>",
            "xl/worksheets/sheet1.xml": f"<?xml version='1.0' encoding='UTF-8'?><worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData><row r='1'><c r='A1' t='inlineStr'><is><t>{text}</t></is></c></row></sheetData></worksheet>",
        },
    )


def pdf(path: Path, label: str) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), f"P10 browser PDF fixture: {label}")
    document.save(path)
    document.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stem = "p10-browser-" + args.label
    paths = {
        "docx": args.output / f"{stem}.docx",
        "xlsx": args.output / f"{stem}.xlsx",
        "pdf": args.output / f"{stem}.pdf",
        "png": args.output / f"{stem}.png",
    }
    docx(paths["docx"], args.label)
    xlsx(paths["xlsx"], args.label)
    pdf(paths["pdf"], args.label)
    paths["png"].write_bytes(PNG_1X1)
    manifest = {
        "label": args.label,
        "files": [
            {"format": key, "path": str(path), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for key, path in paths.items()
        ],
    }
    (args.output / f"{stem}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
