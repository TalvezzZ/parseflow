from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.documents.models import FileInput, ParseContext
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider
from app.skills.excel.store import ExcelStoreWriter, ExcelPageTooLarge, iter_sheet_cells, load_workbook_manifest, read_sheet_page
from app.tasks.exports import generate_document_exports, ExportLimitExceeded


def cell(row, column=1, value='v'):
    return dict(row=row, column=column, value=value, coordinate=f'A{row}', formula=None)


def make_store(tmp_path, values, **kwargs):
    path = tmp_path / 'workbook/manifest.json'
    with ExcelStoreWriter(path, **kwargs) as writer:
        writer.add_sheet(0, {'name': 'Data', 'semantic_cell_count': len(values), 'row_count': len({v['row'] for v in values}), 'semantic_dimension': 'A1:C5'})
        for value in values:
            writer.add_cell(0, value)
        assert not path.exists()
        writer.finish()
    return path


def test_chunk_manifest_hashes_and_atomic_publication(tmp_path):
    path = make_store(tmp_path, [cell(1), cell(3, 2), cell(5, 3)], chunk_records=1)
    manifest = load_workbook_manifest(path)
    assert manifest['schema_version'] == 2
    assert len(manifest['sheets'][0]['chunks']) == 3
    assert not list(tmp_path.rglob('*.sqlite'))
    assert [c['row'] for c in iter_sheet_cells(path, 0)] == [1, 3, 5]
    first = read_sheet_page(path, 0, limit=2)
    assert first['next_after_row'] == 3
    assert read_sheet_page(path, 0, after_row=3)['rows'][0]['row'] == 5


def test_giant_row_spans_chunks_but_page_is_complete_or_413(tmp_path):
    values = [cell(1, col, 'x' * 200) for col in range(1, 8)] + [cell(2)]
    path = make_store(tmp_path, values, chunk_bytes=100)
    assert len(read_sheet_page(path, 0, limit=1, max_bytes=10000)['rows'][0]['cells']) == 7
    with pytest.raises(ExcelPageTooLarge):
        read_sheet_page(path, 0, max_bytes=200)


def test_chunk_hash_tampering_is_detected(tmp_path):
    path = make_store(tmp_path, [cell(1)])
    chunk = load_workbook_manifest(path)['sheets'][0]['chunks'][0]
    file = path.parent / chunk['file']
    content = bytearray(file.read_bytes())
    content[-1] ^= 1
    file.write_bytes(content)
    with pytest.raises(ValueError, match='校验失败'):
        read_sheet_page(path, 0)


def test_chunk_reference_traversal_rejected(tmp_path):
    path = make_store(tmp_path, [cell(1)])
    manifest = load_workbook_manifest(path)
    manifest['sheets'][0]['chunks'][0]['file'] = '../outside.gz'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_sheet_page(path, 0)


def test_failed_write_does_not_publish(tmp_path):
    with pytest.raises(RuntimeError):
        with ExcelStoreWriter(tmp_path / 'workbook/manifest.json') as writer:
            writer.add_sheet(0, {})
            writer.add_cell(0, cell(1))
            raise RuntimeError('cancelled')
    assert list(tmp_path.iterdir()) == []


def test_far_sparse_coordinates_metadata_merge_and_preview_width(tmp_path):
    source = tmp_path / 'far.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet['XFD1'] = 'far-column'
    sheet['A1048576'] = 'far-row'
    sheet['XFD1048576'] = 'corner'
    from openpyxl.comments import Comment
    sheet['B2'].comment = Comment('metadata-only', 'author')
    workbook.save(source)
    output = tmp_path / 'out'
    result = OpenpyxlProvider()._parse_sync(ParseContext(file=FileInput(file_id='far', path=str(source)), metadata={'artifact_dir': str(output)}))
    assert result.status == 'success', result.error
    assert result.document.metadata['excel_storage'] == {'filename': 'workbook/manifest.json', 'schema_version': 2}
    path = output / 'workbook/manifest.json'
    values = list(iter_sheet_cells(path, 0))
    assert [value['coordinate'] for value in values] == ['XFD1', 'B2', 'A1048576', 'XFD1048576']
    assert values[1]['comment'] == 'metadata-only'
    assert all(len(row) <= 100 for row in result.document.tables[0]['rows'])
    page = read_sheet_page(path, 0, after_row=2)
    assert len(page['rows']) == 1 and len(page['rows'][0]['cells']) == 2
    with pytest.raises(ExportLimitExceeded):
        generate_document_exports(result.document.model_dump(), output, max_bytes=100000)
    assert path.exists()


def test_new_chunk_csv_preserves_gaps(tmp_path):
    path = make_store(tmp_path, [cell(1, 1, 'first'), cell(5, 3, '=1+2')])
    document = {'metadata': {'excel_storage': {'filename': 'workbook/manifest.json', 'schema_version': 2}}, 'tables': [], 'representations': {}}
    generate_document_exports(document, tmp_path, max_bytes=10000)
    data = (tmp_path / 'exports/sheet-000.csv').read_text(encoding='utf-8-sig')
    assert data.splitlines() == ['first,,', ',,', ',,', ',,', ",,'=1+2"]
