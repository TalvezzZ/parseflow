"""Generate compact OOXML fixtures and measure isolated Excel parser memory.

Example: .venv/bin/python scripts/benchmark_excel.py --cells 1000000 10000000
Writes fixtures and results only in a temporary directory and removes them on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import zipfile
from pathlib import Path
from time import perf_counter

from app.documents.models import FileInput, ParseContext
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider


def fixture(path: Path, count: int, columns: int = 100, rows_per_sheet: int = 30000) -> None:
    from openpyxl.utils import get_column_letter
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    sheets = (count + columns * rows_per_sheet - 1) // (columns * rows_per_sheet)
    names = [get_column_letter(column) for column in range(1, columns + 1)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/></Types>')
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{package}"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>' + ''.join(f'<sheet name="Sheet{i+1}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i in range(sheets)) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{package}">' + ''.join(f'<Relationship Id="rId{i+1}" Type="{rel}/worksheet" Target="worksheets/sheet{i+1}.xml"/>' for i in range(sheets)) + '</Relationships>')
        remaining = count
        for index in range(sheets):
            with archive.open(f"xl/worksheets/sheet{index+1}.xml", "w") as stream:
                stream.write(f'<worksheet xmlns="{ns}"><sheetData>'.encode())
                for row in range(1, rows_per_sheet + 1):
                    if remaining == 0:
                        break
                    width = min(columns, remaining)
                    stream.write((f'<row r="{row}">' + ''.join(f'<c r="{name}{row}"><v>1</v></c>' for name in names[:width]) + '</row>').encode())
                    remaining -= width
                stream.write(b'</sheetData></worksheet>')


async def run(counts: list[int]) -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix="parseflow-excel-benchmark-") as directory:
        root = Path(directory)
        for count in counts:
            source = root / f"{count}.xlsx"
            started = perf_counter()
            fixture(source, count)
            generation_seconds = round(perf_counter() - started, 3)
            output = root / str(count)
            result = await OpenpyxlProvider().parse(ParseContext(file=FileInput(file_id=f"bench-{count}", path=str(source)), metadata={"artifact_dir": str(output)}))
            actual = result.document.quality.get("semantic_cells") if result.document else 0
            report = {"requested_cells": count, "actual_cells": actual, "status": result.status,
                      "error": result.error.model_dump() if result.error else None,
                      "source_bytes": source.stat().st_size, "generation_seconds": generation_seconds,
                      "sqlite_bytes": (output / "workbook.sqlite").stat().st_size if (output / "workbook.sqlite").exists() else 0,
                      "preview_json_bytes": len(result.model_dump_json().encode()), **result.metrics}
            results.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if result.status != "success" or actual != count:
                raise RuntimeError(f"Benchmark incomplete: {report}")
    print(json.dumps({"results": results}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cells", type=int, nargs="+", default=[100_000, 1_000_000])
    arguments = parser.parse_args()
    if any(count <= 0 or count > 10_000_000 for count in arguments.cells):
        parser.error("cell counts must be between 1 and 10,000,000")
    asyncio.run(run(arguments.cells))
