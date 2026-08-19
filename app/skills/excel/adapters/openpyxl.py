import asyncio
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentResult, ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class ExcelArchiveLimitError(ValueError):
    """XLSX OOXML archive exceeds a configured safety limit."""


class OpenpyxlProvider(Provider):
    """以 OOXML 预扫描与稀疏坐标访问方式解析 XLSX/XLSM。"""

    manifest = ProviderManifest(
        name="excel.normal.openpyxl",
        version="0.4.3",
        kind="spreadsheet_parser",
        modes=["normal"],
        capabilities=["sheets", "sparse_cells", "formulas", "merged_cells", "tables", "images", "markdown", "used_range_hardening"],
    )

    def __init__(
        self,
        max_sheets: int = 30,
        max_semantic_cells: int = 1_000_000,
        max_sheet_rows: int = 100_000,
        max_columns: int = 1_000,
        max_file_size_mb: int = 100,
        max_uncompressed_size_mb: int = 500,
        max_zip_entries: int = 10_000,
        max_sheet_xml_size_mb: int = 100,
    ) -> None:
        self.max_sheets = max_sheets
        self.max_semantic_cells = max_semantic_cells
        self.max_sheet_rows = max_sheet_rows
        self.max_columns = max_columns
        self.max_file_size_mb = max_file_size_mb
        self.max_uncompressed_size_mb = max_uncompressed_size_mb
        self.max_zip_entries = max_zip_entries
        self.max_sheet_xml_size_mb = max_sheet_xml_size_mb

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        try:
            scan = self._pre_scan(source)
        except ExcelArchiveLimitError as exc:
            return self._failed("excel_archive_limit_exceeded", str(exc))
        except Exception as exc:
            return self._failed("excel_prescan_failed", str(exc))
        try:
            from openpyxl import load_workbook
        except ImportError:
            return self._failed("provider_unavailable", "未安装 openpyxl")
        try:
            workbook = load_workbook(source, data_only=False, read_only=False, keep_vba=False)
        except Exception as exc:
            return self._failed("provider_failed", str(exc))

        artifact_dir = Path(str(context.metadata.get("artifact_dir") or tempfile.mkdtemp(prefix=f"parse-agent-{context.file.file_id}-xlsx-")))
        images_dir = artifact_dir / "images"
        total_cells = 0
        sheets: list[dict[str, Any]] = []
        blocks: list[DocumentBlock] = []
        tables: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        markdown_parts: list[str] = []
        warnings: list[str] = []
        truncated = False
        try:
            visible_sheets = [sheet for sheet in workbook.worksheets if sheet.sheet_state != "hidden"]
            for sheet_index, sheet in enumerate(visible_sheets):
                if sheet_index >= self.max_sheets:
                    truncated = True
                    warnings.append(f"工作表数量超过上限 {self.max_sheets}，后续工作表未解析")
                    break
                workbook_sheet_index = workbook.worksheets.index(sheet)
                xml_path = scan["sheet_paths"][workbook_sheet_index] if workbook_sheet_index < len(scan["sheet_paths"]) else ""
                semantic_coordinates = scan["cells"].get(xml_path, set())
                declared_dimension = scan["dimensions"].get(xml_path, "")
                cells: list[dict[str, Any]] = []
                skipped = 0
                for coordinate in sorted(semantic_coordinates, key=self._coordinate_sort_key):
                    row, column = self._coordinate_to_tuple(coordinate)
                    if row > self.max_sheet_rows or column > self.max_columns or total_cells >= self.max_semantic_cells:
                        skipped += 1
                        truncated = True
                        continue
                    cell = sheet[coordinate]
                    value = cell.value
                    # XML may include a shared string/formula cell whose value resolves to None; retain only metadata-bearing cells.
                    if value is None and not cell.comment and not cell.hyperlink:
                        continue
                    total_cells += 1
                    formula = value if isinstance(value, str) and value.startswith("=") else None
                    cells.append({
                        "coordinate": cell.coordinate,
                        "row": cell.row,
                        "column": cell.column,
                        "value": self._json_value(value),
                        "formula": formula,
                        "comment": cell.comment.text if cell.comment else None,
                        "hyperlink": cell.hyperlink.target if cell.hyperlink else None,
                    })
                if skipped:
                    warnings.append(f"工作表 {sheet.title} 有 {skipped} 个语义单元格超过解析预算，已跳过")
                merged = [str(rng) for rng in sheet.merged_cells.ranges]
                semantic_dimension = self._semantic_dimension(cells)
                if self._is_polluted_dimension(declared_dimension, semantic_dimension):
                    warnings.append(f"excel_used_range_polluted: 工作表 {sheet.title} 声明范围 {declared_dimension} 大于语义范围 {semantic_dimension}")
                sheet_info = {
                    "name": sheet.title,
                    "state": sheet.sheet_state,
                    "cells": cells,
                    "merged_ranges": merged,
                    "declared_dimension": declared_dimension,
                    "semantic_dimension": semantic_dimension,
                    "semantic_cell_count": len(cells),
                }
                sheets.append(sheet_info)
                if cells:
                    blocks.append(DocumentBlock(kind="heading", text=sheet.title, metadata={"sheet": sheet.title}))
                    rows: dict[int, list[dict[str, Any]]] = {}
                    for item in cells:
                        rows.setdefault(item["row"], []).append(item)
                    row_lines = []
                    for row_number, row_cells in sorted(rows.items()):
                        row_cells.sort(key=lambda item: item["column"])
                        row_text = " | ".join(str(item["value"] if item["value"] is not None else "") for item in row_cells)
                        row_lines.append(row_text)
                        blocks.append(DocumentBlock(kind="text", text=row_text, metadata={"sheet": sheet.title, "row": row_number}))
                    table = {"sheet_name": sheet.title, "range": semantic_dimension, "declared_range": declared_dimension,
                             "rows": row_lines, "cells": cells, "merged_ranges": merged}
                    tables.append(table)
                    markdown_parts.append(f"## {sheet.title}\n\n" + "\n".join(f"- {line}" for line in row_lines))
                if getattr(sheet, "_images", None):
                    images_dir.mkdir(parents=True, exist_ok=True)
                    for image_index, image in enumerate(sheet._images, start=1):
                        path = images_dir / f"{sheet.title}-{image_index:04d}.png"
                        path.write_bytes(image._data())
                        item = {"sheet": sheet.title, "filename": path.name, "path": str(path), "relative_path": f"images/{path.name}"}
                        images.append(item)
                        blocks.append(DocumentBlock(kind="image", metadata=item))
        finally:
            workbook.close()

        if not any(sheet["cells"] for sheet in sheets) and not images:
            return ProviderResult(status="failed", provider_name=self.manifest.name, warnings=warnings,
                                  error=ProviderError(code="quality_insufficient", message="Excel 未提取到语义内容", retryable=False))
        document = DocumentResult(
            document_id=context.file.file_id,
            source_file=context.file,
            document_type="xlsx",
            blocks=blocks,
            tables=tables,
            images=images,
            representations={"plain_text": "\n".join(block.text for block in blocks if block.text), "markdown": "\n\n".join(markdown_parts)},
            metadata={"artifact_dir": str(artifact_dir), "sheet_count": len(sheets), "archive": scan["archive"]},
            parser={"name": self.manifest.name, "version": self.manifest.version},
            warnings=warnings,
            quality={"sheets": len(sheets), "semantic_cells": total_cells, "tables": len(tables), "images": len(images), "truncated": str(truncated)},
            provenance={"sheets": sheets},
            extensions={"workbook": {"sheets": sheets}},
        )
        return ProviderResult(status="partial" if truncated else "success", provider_name=self.manifest.name,
                              document=document, warnings=warnings, metrics=document.quality)

    def _pre_scan(self, source: Path) -> dict[str, Any]:
        if source.stat().st_size > self.max_file_size_mb * 1024 * 1024:
            raise ExcelArchiveLimitError(f"Excel 文件超过 {self.max_file_size_mb} MB 限制")
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > self.max_zip_entries:
                raise ExcelArchiveLimitError(f"Excel ZIP entry 数量超过 {self.max_zip_entries} 限制")
            uncompressed_size = sum(info.file_size for info in infos)
            if uncompressed_size > self.max_uncompressed_size_mb * 1024 * 1024:
                raise ExcelArchiveLimitError(f"Excel 解压后大小超过 {self.max_uncompressed_size_mb} MB 限制")
            sheet_paths = sorted((info.filename for info in infos if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml")), key=self._sheet_path_sort_key)
            if len(sheet_paths) > self.max_sheets:
                # The parser returns partial for visible sheets, but refuse excessive XML before loading it.
                sheet_paths = sheet_paths[:self.max_sheets]
            cells: dict[str, set[str]] = {}
            dimensions: dict[str, str] = {}
            for sheet_path in sheet_paths:
                info = archive.getinfo(sheet_path)
                if info.file_size > self.max_sheet_xml_size_mb * 1024 * 1024:
                    raise ExcelArchiveLimitError(f"工作表 XML {sheet_path} 超过 {self.max_sheet_xml_size_mb} MB 限制")
                coordinates, dimension = self._scan_sheet_xml(archive, sheet_path)
                cells[sheet_path] = coordinates
                dimensions[sheet_path] = dimension
            return {"sheet_paths": sheet_paths, "cells": cells, "dimensions": dimensions, "archive": {"entries": len(infos), "uncompressed_size": uncompressed_size}}

    @staticmethod
    def _scan_sheet_xml(archive: zipfile.ZipFile, sheet_path: str) -> tuple[set[str], str]:
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        coordinates: set[str] = set()
        dimension = ""
        with archive.open(sheet_path) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag == f"{namespace}dimension":
                    dimension = element.attrib.get("ref", "")
                elif element.tag == f"{namespace}c":
                    coordinate = element.attrib.get("r")
                    has_value = element.find(f"{namespace}v") is not None
                    has_formula = element.find(f"{namespace}f") is not None
                    has_inline_string = element.find(f"{namespace}is") is not None
                    if coordinate and (has_value or has_formula or has_inline_string):
                        coordinates.add(coordinate)
                    element.clear()
        return coordinates, dimension

    @staticmethod
    def _sheet_path_sort_key(path: str) -> tuple[int, str]:
        stem = Path(path).stem
        digits = "".join(character for character in stem if character.isdigit())
        return (int(digits) if digits else 0, path)

    @staticmethod
    def _coordinate_to_tuple(coordinate: str) -> tuple[int, int]:
        from openpyxl.utils.cell import coordinate_to_tuple
        return coordinate_to_tuple(coordinate)

    @staticmethod
    def _coordinate_sort_key(coordinate: str) -> tuple[int, int]:
        return OpenpyxlProvider._coordinate_to_tuple(coordinate)

    @staticmethod
    def _semantic_dimension(cells: list[dict[str, Any]]) -> str:
        if not cells:
            return ""
        from openpyxl.utils import get_column_letter
        min_row = min(cell["row"] for cell in cells)
        max_row = max(cell["row"] for cell in cells)
        min_column = min(cell["column"] for cell in cells)
        max_column = max(cell["column"] for cell in cells)
        return f"{get_column_letter(min_column)}{min_row}:{get_column_letter(max_column)}{max_row}"

    @staticmethod
    def _is_polluted_dimension(declared: str, semantic: str) -> bool:
        if not declared or not semantic or declared == semantic:
            return False
        try:
            from openpyxl.utils.cell import range_boundaries
            min_col, min_row, max_col, max_row = range_boundaries(declared)
            semantic_min_col, semantic_min_row, semantic_max_col, semantic_max_row = range_boundaries(semantic)
            declared_area = (max_col - min_col + 1) * (max_row - min_row + 1)
            semantic_area = (semantic_max_col - semantic_min_col + 1) * (semantic_max_row - semantic_min_row + 1)
            return declared_area > max(semantic_area * 100, 10_000)
        except ValueError:
            return False

    def _failed(self, code: str, message: str) -> ProviderResult:
        return ProviderResult(status="failed", provider_name=self.manifest.name,
                              error=ProviderError(code=code, message=message, retryable=False))

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return value
