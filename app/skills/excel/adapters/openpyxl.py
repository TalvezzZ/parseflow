from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentResult, ParseContext, ProviderError
from app.skills.excel.streaming import local, records, relationships
from app.skills.providers import Provider, ProviderManifest, ProviderResult


def _string_text(element) -> str:
    """OOXML plain/rich text excludes phonetic rPh annotation runs."""
    parts = []
    for node in element:
        if local(node.tag) == "t":
            parts.append(node.text or "")
        elif local(node.tag) == "r":
            parts.extend(child.text or "" for child in node if local(child.tag) == "t")
    return "".join(parts)


class ExcelArchiveLimitError(ValueError):
    """XLSX OOXML archive exceeds a configured safety limit."""


class OpenpyxlProvider(Provider):
    """Stream OOXML into SQLite, retaining only bounded document previews."""
    manifest = ProviderManifest(name="excel.normal.openpyxl", version="0.5.0", kind="spreadsheet_parser", modes=["normal"],
        capabilities=["sheets", "sparse_cells", "formulas", "merged_cells", "tables", "images", "markdown", "used_range_hardening"])

    def __init__(self, max_sheets: int = 30, max_semantic_cells: int = 10_000_000,
                 max_sheet_rows: int = 100_000, max_columns: int = 1_000,
                 max_file_size_mb: int = 100, max_uncompressed_size_mb: int = 500,
                 max_zip_entries: int = 10_000, max_sheet_xml_size_mb: int = 100,
                 max_preview_cells: int = 2000, max_preview_bytes: int = 1_000_000,
                 max_preview_rows: int = 100, max_storage_mb: int = 4096) -> None:
        for name, value in locals().copy().items():
            if name != "self":
                setattr(self, name, max(0, int(value)))

    async def parse(self, context: ParseContext) -> ProviderResult:
        from app.skills.excel.process import parse_in_worker
        return await parse_in_worker(context, dict(vars(self)))

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        try:
            return self._read(context)
        except ExcelArchiveLimitError as exc:
            return self._failed("excel_archive_limit_exceeded", str(exc))
        except ImportError as exc:
            return self._failed("provider_unavailable", str(exc))
        except Exception as exc:
            return self._failed("provider_failed", str(exc))

    def _read(self, context: ParseContext) -> ProviderResult:
        from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries
        from openpyxl.formula.translate import Translator
        from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format, is_timedelta_format
        from openpyxl.utils.datetime import from_excel, from_ISO8601, CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900
        from app.skills.excel.store import ExcelStoreWriter

        source = Path(context.file.path)
        scan = self._pre_scan(source)
        artifact_dir = Path(str(context.metadata.get("artifact_dir") or tempfile.mkdtemp(prefix=f"parse-agent-{context.file.file_id}-xlsx-")))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        sheets, tables, images, blocks, markdown, warnings = [], [], [], [], [], []
        total_cells = truncated = preview_cells = 0
        truncation_reasons: set[str] = set()
        image_bytes = 0
        preview_remaining = self.max_preview_bytes
        metadata_remaining = 1_000_000
        metadata_entries = 0

        def metadata_accept(value):
            nonlocal metadata_remaining, metadata_entries, truncated
            size = len(json.dumps(value, ensure_ascii=True).encode())
            if metadata_entries >= 10000 or size > metadata_remaining:
                truncation_reasons.add("metadata_budget")
                truncated += 1
                return False
            metadata_entries += 1
            metadata_remaining -= size
            return True

        with zipfile.ZipFile(source) as archive, ExcelStoreWriter(artifact_dir / "workbook.sqlite", max_bytes=self.max_storage_mb * 1024 * 1024) as store:
            db = store.connection
            db.executescript("""
                CREATE TABLE x_strings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE x_formats (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE x_styles (id INTEGER PRIMARY KEY, format INTEGER NOT NULL);
                CREATE TABLE x_formulas (id TEXT PRIMARY KEY, origin TEXT, formula TEXT);
                CREATE TABLE x_metadata (row INTEGER, col INTEGER, payload TEXT, PRIMARY KEY(row,col)) WITHOUT ROWID;
            """)
            rels = relationships(archive, "xl/workbook.xml")
            book_sheets = []
            epoch = CALENDAR_WINDOWS_1900
            for element, _ in records(archive, "xl/workbook.xml", {"sheet", "workbookPr"}):
                if local(element.tag) == "workbookPr":
                    if element.get("date1904") in ("1", "true"):
                        epoch = CALENDAR_MAC_1904
                elif element.get("state", "visible") == "visible":
                    if len(book_sheets) >= self.max_sheets:
                        truncation_reasons.add("sheet_limit")
                        truncated += 1
                        continue
                    rid = next((v for k, v in element.attrib.items() if local(k) == "id"), "")
                    rel = rels.get(rid)
                    if rel and rel["type"] == "worksheet" and not rel["external"]:
                        book_sheets.append((element.get("name", "")[:1024], rel["target"]))
            shared = next((r["target"] for r in rels.values() if r["type"] == "sharedStrings" and not r["external"]), None)
            if shared:
                for index, (element, _) in enumerate(records(archive, shared, {"si"})):
                    value = _string_text(element)
                    db.execute("INSERT INTO x_strings VALUES (?,?)", (index, value))
                    if index % 2048 == 0:
                        db.commit()
                        store.check_budget()
            style_path = next((r["target"] for r in rels.values() if r["type"] == "styles" and not r["external"]), None)
            if style_path:
                index = 0
                for element, parent in records(archive, style_path, {"numFmt", "xf"}):
                    if local(element.tag) == "numFmt":
                        db.execute("INSERT OR REPLACE INTO x_formats VALUES (?,?)", (int(element.get("numFmtId", "0")), element.get("formatCode", "")))
                    elif parent == "cellXfs":
                        db.execute("INSERT INTO x_styles VALUES (?,?)", (index, int(element.get("numFmtId", "0"))))
                        index += 1
            db.commit()

            def typed_value(text, kind, style):
                if text is None:
                    return None
                if kind == "s":
                    item = db.execute("SELECT value FROM x_strings WHERE id=?", (int(text),)).fetchone()
                    return item[0] if item else None
                if kind == "b":
                    return text == "1"
                if kind == "d":
                    return self._json_value(from_ISO8601(text))
                if kind in ("str", "inlineStr", "e"):
                    return text
                try:
                    value = float(text) if any(char in text for char in ".eE") else int(text)
                except ValueError:
                    return text
                if style is not None:
                    fmt = db.execute("SELECT format FROM x_styles WHERE id=?", (int(style),)).fetchone()
                    if fmt:
                        custom = db.execute("SELECT value FROM x_formats WHERE id=?", (fmt[0],)).fetchone()
                        code = custom[0] if custom else BUILTIN_FORMATS.get(fmt[0], "")
                        if is_date_format(code) or is_timedelta_format(code):
                            try:
                                return self._json_value(from_excel(value, epoch, timedelta=is_timedelta_format(code)))
                            except (ValueError, OverflowError):
                                pass
                return value

            for sheet_id, (name, path) in enumerate(book_sheets):
                db.execute("DELETE FROM x_metadata")
                db.execute("DELETE FROM x_formulas")
                sheet_rels = relationships(archive, path)
                info = {"sheet_id": sheet_id, "name": name, "sheet_name": name, "state": "visible"}
                store.add_sheet(sheet_id, info)
                merged = []
                declared = ""

                def put_metadata(row, col, additions):
                    if row < 1 or col < 1 or row > self.max_sheet_rows or col > self.max_columns:
                        return False
                    old = db.execute("SELECT payload FROM x_metadata WHERE row=? AND col=?", (row, col)).fetchone()
                    payload = json.loads(old[0]) if old else {}
                    payload.update(additions)
                    db.execute("INSERT OR REPLACE INTO x_metadata VALUES (?,?,?)", (row, col, json.dumps(payload)))
                    return True

                for rel in sheet_rels.values():
                    if rel["type"] == "comments" and not rel["external"]:
                        for element, _ in records(archive, rel["target"], {"comment"}):
                            text = "".join(node.text or "" for node in element.iter() if local(node.tag) == "t")
                            item = {"comment": text}
                            if metadata_accept(item):
                                row, col = coordinate_to_tuple(element.get("ref", "A1"))
                                if not put_metadata(row, col, item):
                                    truncation_reasons.add("row_or_column_limit")
                                    truncated += 1
                for element, parent in records(archive, path, {"dimension", "mergeCell", "hyperlink", "f"}, with_parent=True):
                    tag = local(element.tag)
                    if tag == "f":
                        if element.get("t") == "shared" and element.get("si") is not None and element.text and parent.get("r"):
                            db.execute("INSERT OR REPLACE INTO x_formulas VALUES (?,?,?)", (element.get("si"), parent.get("r"), "=" + element.text))
                    elif tag == "dimension":
                        declared = element.get("ref", "")[:128]
                    elif tag == "mergeCell":
                        ref = element.get("ref", "")
                        if metadata_accept(ref):
                            merged.append(ref)
                    else:
                        rid = next((v for k, v in element.attrib.items() if local(k) == "id"), "")
                        link = sheet_rels.get(rid, {}).get("target") or element.get("location")
                        item = {"hyperlink": link}
                        if not metadata_accept(item):
                            continue
                        left, top, right, bottom = range_boundaries(element.get("ref", "A1"))
                        expansion = 0
                        for row in range(top, min(bottom, self.max_sheet_rows) + 1):
                            for col in range(left, min(right, self.max_columns) + 1):
                                if expansion >= 10000 or metadata_entries >= 10000:
                                    break
                                put_metadata(row, col, item)
                                expansion += 1
                                metadata_entries += 1
                            if expansion >= 10000 or metadata_entries >= 10000:
                                break
                        if expansion < (right-left+1)*(bottom-top+1):
                            truncation_reasons.add("metadata_or_dimension_limit")
                            truncated += 1
                db.commit()
                has_metadata = bool(db.execute("SELECT 1 FROM x_metadata LIMIT 1").fetchone())
                bounds = [self.max_sheet_rows + 1, self.max_columns + 1, 0, 0]
                count = 0

                def emit(cell):
                    nonlocal count, total_cells, truncated
                    row, col = cell["row"], cell["column"]
                    if row < 1 or col < 1 or row > self.max_sheet_rows or col > self.max_columns or total_cells >= self.max_semantic_cells:
                        truncation_reasons.add("cell_limit" if total_cells >= self.max_semantic_cells else "row_or_column_limit")
                        truncated += 1
                        return
                    store.add_cell(sheet_id, cell)
                    count += 1
                    total_cells += 1
                    bounds[0] = min(bounds[0], row)
                    bounds[1] = min(bounds[1], col)
                    bounds[2] = max(bounds[2], row)
                    bounds[3] = max(bounds[3], col)

                row_index = col_index = 0
                last_parent = None
                for node, parent in records(archive, path, {"c"}, with_parent=True):
                    if parent is not last_parent:
                        row_index = int(parent.get("r", row_index + 1))
                        col_index = 0
                        last_parent = parent
                    coordinate = node.get("r")
                    if coordinate:
                        row, col = coordinate_to_tuple(coordinate)
                    else:
                        row, col = row_index, col_index + 1
                        coordinate = f"{get_column_letter(col)}{row}"
                    col_index = col
                    parts = {local(child.tag): child for child in node}
                    formula_node = parts.get("f")
                    value_node = parts.get("v")
                    kind = node.get("t", "n")
                    text = value_node.text if value_node is not None else None
                    if kind == "inlineStr":
                        inline = parts.get("is")
                        text = _string_text(inline) if inline is not None else ""
                    formula = None
                    if formula_node is not None:
                        formula_text = formula_node.text
                        shared_id = formula_node.get("si")
                        if formula_text is not None:
                            formula = "=" + formula_text
                            if formula_node.get("t") == "shared" and shared_id is not None:
                                db.execute("INSERT OR REPLACE INTO x_formulas VALUES (?,?,?)", (shared_id, coordinate, formula))
                        elif shared_id is not None:
                            origin = db.execute("SELECT origin,formula FROM x_formulas WHERE id=?", (shared_id,)).fetchone()
                            if origin:
                                formula = Translator(origin[1], origin=origin[0]).translate_formula(coordinate)
                    extra = db.execute("SELECT payload FROM x_metadata WHERE row=? AND col=?", (row, col)).fetchone() if has_metadata else None
                    if extra:
                        db.execute("DELETE FROM x_metadata WHERE row=? AND col=?", (row, col))
                    if formula_node is None and text is None and not extra:
                        continue
                    value = typed_value(text, kind, node.get("s"))
                    cell = {"coordinate": coordinate, "row": row, "column": col,
                            "value": formula if formula is not None else value, "formula": formula,
                            "comment": None, "hyperlink": None}
                    if formula_node is not None:
                        cell["cached_value"] = value
                    if extra:
                        cell.update(json.loads(extra[0]))
                    emit(cell)
                for row, col, payload in db.execute("SELECT row,col,payload FROM x_metadata ORDER BY row,col"):
                    cell = {"coordinate": f"{get_column_letter(col)}{row}", "row": row, "column": col,
                            "value": None, "formula": None, "comment": None, "hyperlink": None}
                    cell.update(json.loads(payload))
                    emit(cell)
                store.flush()
                row_count = db.execute("SELECT COUNT(DISTINCT row) FROM cells WHERE sheet_id=?", (sheet_id,)).fetchone()[0]
                dimension = f"{get_column_letter(bounds[1])}{bounds[0]}:{get_column_letter(bounds[3])}{bounds[2]}" if count else ""
                if self._is_polluted_dimension(declared, dimension):
                    warnings.append(f"excel_used_range_polluted: 工作表 {name} 声明范围 {declared} 大于语义范围 {dimension}")
                info.update(merged_ranges=merged, declared_dimension=declared, semantic_dimension=dimension,
                            semantic_cell_count=count, row_count=row_count)
                cells, rows = [], []
                for payload, in db.execute("SELECT payload FROM cells WHERE sheet_id=? AND row<=? ORDER BY row,column", (sheet_id, self.max_preview_rows)):
                    cell = json.loads(payload)
                    slots = max(0, cell["row"] - len(rows)) + max(0, cell["column"] - (len(rows[cell["row"]-1]) if cell["row"] <= len(rows) else 0))
                    cost = len(payload.encode()) * 8 + slots * 8 + 128
                    if preview_cells >= self.max_preview_cells or cost > preview_remaining:
                        break
                    preview_remaining -= cost
                    preview_cells += 1
                    cells.append(cell)
                    while len(rows) < cell["row"]:
                        rows.append([])
                    line = rows[cell["row"]-1]
                    line.extend([None] * (cell["column"] - len(line)))
                    line[cell["column"]-1] = cell["value"]
                info["preview_truncated"] = len(cells) < count
                store.update_sheet(sheet_id, info)
                sheets.append(info)
                tables.append(dict(info, range=dimension, declared_range=declared, cells=cells, rows=rows, data_ref="workbook.sqlite"))
                if cells:
                    blocks.append(DocumentBlock(kind="heading", text=name, metadata={"sheet": name, "sheet_id": sheet_id}))
                    lines = []
                    for number, row in enumerate(rows, 1):
                        text = " | ".join(str(value) if value is not None else "" for value in row)
                        lines.append(text)
                        blocks.append(DocumentBlock(kind="text", text=text, metadata={"sheet": name, "row": number}))
                    markdown.append(f"## {name}\n\n" + "\n".join(f"- {line}" for line in lines))
                for rel in sheet_rels.values():
                    if rel["type"] != "drawing" or rel["external"]:
                        continue
                    drawing_rels = relationships(archive, rel["target"])
                    for element, _ in records(archive, rel["target"], {"oneCellAnchor", "twoCellAnchor", "absoluteAnchor"}):
                        for node in element.iter():
                            if local(node.tag) != "blip":
                                continue
                            rid = next((v for k, v in node.attrib.items() if local(k) == "embed"), "")
                            media = drawing_rels.get(rid)
                            if not media or media["external"]:
                                continue
                            suffix = Path(media["target"]).suffix.lower()
                            if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".emf", ".wmf", ".svg"}:
                                suffix = ".bin"
                            filename = f"sheet-{sheet_id}-{len(images)+1:04d}{suffix}"
                            item = {"sheet": name, "sheet_id": sheet_id, "filename": filename, "path": str(artifact_dir / "images" / filename), "relative_path": f"images/{filename}"}
                            anchor = next((child for child in element if local(child.tag) == "from"), None)
                            if anchor is not None:
                                item["anchor"] = {local(child.tag): int(child.text or 0) for child in anchor}
                            if not metadata_accept(item):
                                continue
                            image_bytes += archive.getinfo(media["target"]).file_size
                            database_bytes = db.execute("PRAGMA page_count").fetchone()[0] * 4096
                            if image_bytes + database_bytes > self.max_storage_mb * 1024 * 1024:
                                raise ValueError("Excel 数据和图片存储超过磁盘预算")
                            (artifact_dir / "images").mkdir(exist_ok=True)
                            with archive.open(media["target"]) as src, (artifact_dir / "images" / filename).open("wb") as dst:
                                shutil.copyfileobj(src, dst, length=65536)
                            images.append(item)
                            blocks.append(DocumentBlock(kind="image", metadata=item))
            for table_name in ("x_strings", "x_formats", "x_styles", "x_formulas", "x_metadata"):
                db.execute(f"DROP TABLE {table_name}")
            store.flush()
            if db.execute("PRAGMA page_count").fetchone()[0] * 4096 + image_bytes > self.max_storage_mb * 1024 * 1024:
                raise ValueError("Excel 数据和图片存储超过磁盘预算")
            store.finish()
        if not total_cells and not images:
            return self._failed("quality_insufficient", "Excel 未提取到语义内容")
        if truncated:
            warnings.append(f"excel_parse_truncated: 跳过 {truncated} 个超出预算的单元格或元数据项，原因: {', '.join(sorted(truncation_reasons))}")
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="xlsx",
            blocks=blocks, tables=tables, images=images,
            representations={"plain_text": "\n".join(block.text for block in blocks if block.text), "markdown": "\n\n".join(markdown)},
            metadata={"artifact_dir": str(artifact_dir), "sheet_count": len(sheets), "archive": scan["archive"], "excel_storage": {"filename": "workbook.sqlite", "schema_version": 1}, "sheets": sheets},
            parser={"name": self.manifest.name, "version": self.manifest.version}, warnings=warnings,
            quality={"sheets": len(sheets), "semantic_cells": total_cells, "tables": len(tables), "images": len(images), "truncated": int(truncated), "truncation_reason": ",".join(sorted(truncation_reasons)), "preview_truncated": int(any(sheet["preview_truncated"] for sheet in sheets))},
            provenance={"sheets": sheets}, extensions={"workbook": {"sheets": sheets}})
        return ProviderResult(status="partial" if truncated else "success", provider_name=self.manifest.name, document=document, warnings=warnings, metrics=document.quality)

    def _pre_scan(self, source: Path) -> dict[str, Any]:
        if source.stat().st_size > self.max_file_size_mb * 1024 * 1024:
            raise ExcelArchiveLimitError(f"Excel 文件超过 {self.max_file_size_mb} MB 限制")
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > self.max_zip_entries:
                raise ExcelArchiveLimitError(f"Excel ZIP entry 数量超过 {self.max_zip_entries} 限制")
            total = sum(info.file_size for info in infos)
            if total > self.max_uncompressed_size_mb * 1024 * 1024:
                raise ExcelArchiveLimitError(f"Excel 解压后大小超过 {self.max_uncompressed_size_mb} MB 限制")
            for info in infos:
                if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml") and info.file_size > self.max_sheet_xml_size_mb * 1024 * 1024:
                    raise ExcelArchiveLimitError(f"工作表 XML {info.filename} 超过 {self.max_sheet_xml_size_mb} MB 限制")
            return {"archive": {"entries": len(infos), "uncompressed_size": total}}

    @staticmethod
    def _is_polluted_dimension(declared: str, semantic: str) -> bool:
        if not declared or not semantic or declared == semantic:
            return False
        try:
            from openpyxl.utils.cell import range_boundaries
            a, b, c, d = range_boundaries(declared)
            e, f, g, h = range_boundaries(semantic)
            return (c-a+1)*(d-b+1) > max((g-e+1)*(h-f+1)*100, 10000)
        except (ValueError, TypeError):
            return False

    def _failed(self, code: str, message: str) -> ProviderResult:
        return ProviderResult(status="failed", provider_name=self.manifest.name, error=ProviderError(code=code, message=message, retryable=False))

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, timedelta):
            return value.total_seconds()
        return value
