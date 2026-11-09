from __future__ import annotations

import io
from contextlib import contextmanager

import pytest
from defusedxml.common import DTDForbidden

from app.skills.excel.streaming import (
    _BoundedTreeBuilder,
    _XML_MAX_BYTES,
    records,
)


class MemoryArchive:
    def __init__(self, content: bytes):
        self.content = content
        self.bytes_read = 0
        self.largest_read = 0
        self.closed = False

    @contextmanager
    def open(self, path):
        archive = self

        class TrackedStream(io.BytesIO):
            def read(self, size=-1):
                archive.largest_read = max(archive.largest_read, size)
                chunk = super().read(size)
                archive.bytes_read += len(chunk)
                return chunk

        with TrackedStream(self.content) as stream:
            try:
                yield stream
            finally:
                self.closed = True


def snapshots(xml: bytes, names=frozenset({'si'})):
    archive = MemoryArchive(xml)
    return [(element.tag, ''.join(element.itertext()), parent)
            for element, parent in records(archive, 'part.xml', set(names))]


def test_long_text_is_rejected_before_treebuilder_accumulates_it():
    builder = _BoundedTreeBuilder({'si'})
    builder.start('si', {})
    builder.start('t', {})
    with pytest.raises(ValueError, match='element.*safety budget'):
        builder.data('x' * (_XML_MAX_BYTES + 1))
    assert builder.end('t').text is None


@pytest.mark.parametrize('value', ['x', '汉'])
def test_streaming_long_text_has_byte_limit_and_closes_stream(value):
    archive = MemoryArchive(b'<sst><si><t>' + (value * (_XML_MAX_BYTES + 1)).encode() + b'</t></si></sst>')
    with pytest.raises(ValueError, match='safety budget'):
        list(records(archive, 'part.xml', {'si'}))
    assert archive.bytes_read <= _XML_MAX_BYTES + 2 * 16384
    assert archive.largest_read <= 16384
    assert archive.closed


@pytest.mark.parametrize('xml', [
    b'<sst><si value="' + b'x' * (_XML_MAX_BYTES + 1) + b'"/></sst>',
    b'<sst><!--' + b'x' * (_XML_MAX_BYTES + 1) + b'--><si/></sst>',
    b'<sst><si value="' + b'>' * (_XML_MAX_BYTES + 1) + b'"/></sst>',
])
def test_unfinished_xml_tokens_are_rejected_before_expat_buffers_more(xml):
    archive = MemoryArchive(xml)
    with pytest.raises(ValueError, match='unfinished token.*safety budget'):
        list(records(archive, 'part.xml', {'si'}))
    assert archive.bytes_read <= _XML_MAX_BYTES + 16384
    assert archive.largest_read <= 16384
    assert archive.closed


def test_unfinished_attribute_guard_also_handles_utf16():
    xml = ('<sst><si value="' + 'x' * (_XML_MAX_BYTES // 2 + 1) + '"/></sst>').encode('utf-16')
    with pytest.raises(ValueError, match='unfinished token.*safety budget'):
        snapshots(xml)


def test_depth_is_checked_even_outside_selected_records():
    assert snapshots(b'<n>' * 128 + b'</n>' * 128) == []
    with pytest.raises(ValueError, match='depth safety budget'):
        snapshots(b'<n>' * 129 + b'</n>' * 129)


def test_record_node_limit_precedes_new_node_accumulation():
    builder = _BoundedTreeBuilder({'si'})
    builder.start('si', {})
    for _ in range(65535):
        builder.start('r', {})
        builder.end('r')
    with pytest.raises(ValueError, match='node safety budget'):
        builder.start('r', {})
    assert len(builder.end('si')) == 65535


def test_record_byte_budget_combines_children_and_attributes():
    part = b'x' * (_XML_MAX_BYTES // 2)
    xml = b'<sst><si a="x"><r><t>' + part + b'</t></r><r><t>' + part + b'</t></r></si></sst>'
    with pytest.raises(ValueError, match='record.*safety budget'):
        snapshots(xml)


def test_exact_text_limit_and_independent_records_remain_supported():
    text = b'x' * _XML_MAX_BYTES
    result = snapshots(b'<sst><si><t>' + text + b'</t></si><si><t>second</t></si></sst>')
    assert result == [('si', text.decode(), 'sst'), ('si', 'second', 'sst')]


def test_parent_interface_retains_record_children_and_removes_completed_nodes():
    archive = MemoryArchive(b'<root><row r="1"><c r="A1"><v>1</v></c><c r="B1"><v>2</v></c></row></root>')
    iterator = records(archive, 'part.xml', {'c'}, with_parent=True)
    first, parent = next(iterator)
    assert first.find('v').text == '1'
    assert parent.tag == 'row' and parent.get('r') == '1'
    second, second_parent = next(iterator)
    assert second_parent is parent
    assert first not in parent and not list(first)
    assert second.find('v').text == '2'
    assert list(iterator) == []
    assert archive.closed


def test_dtd_protection_remains_enabled():
    with pytest.raises(DTDForbidden):
        snapshots(b'<!DOCTYPE sst [<!ENTITY text "secret">]><sst><si>&text;</si></sst>')
