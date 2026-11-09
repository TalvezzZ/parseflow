"""Bounded OOXML primitives; no workbook object or coordinate index in memory."""
from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from xml.etree.ElementTree import TreeBuilder

from defusedxml.ElementTree import DefusedXMLParser, iterparse


_XML_MAX_BYTES = 4 * 1024 * 1024
_XML_MAX_DEPTH = 128
_XML_MAX_NODES = 65536
_XML_CHUNK_BYTES = 16384


class _BoundedTreeBuilder(TreeBuilder):
    """Reject oversized content before TreeBuilder retains text or new nodes."""

    def __init__(self, names: set[str]):
        super().__init__()
        self.names = names
        self.element_bytes: list[int] = []
        self.record_depth: int | None = None
        self.record_bytes = 0
        self.record_nodes = 0

    def _account(self, size: int) -> None:
        if self.element_bytes:
            if self.element_bytes[-1] + size > _XML_MAX_BYTES:
                raise ValueError('OOXML element exceeds 4 MB safety budget')
            self.element_bytes[-1] += size
        if self.record_depth is not None:
            if self.record_bytes + size > _XML_MAX_BYTES:
                raise ValueError('OOXML record exceeds safety budget')
            self.record_bytes += size

    def start(self, tag, attrs):
        if len(self.element_bytes) >= _XML_MAX_DEPTH:
            raise ValueError('OOXML nesting exceeds depth safety budget')
        if self.record_depth is None and local(tag) in self.names:
            self.record_depth = len(self.element_bytes) + 1
            self.record_bytes = self.record_nodes = 0
        if self.record_depth is not None:
            if self.record_nodes >= _XML_MAX_NODES:
                raise ValueError('OOXML record exceeds node safety budget')
            self.record_nodes += 1
        self.element_bytes.append(0)
        self._account(sum(len(key.encode('utf-8')) + len(value.encode('utf-8'))
                          for key, value in attrs.items()))
        return super().start(tag, attrs)

    def data(self, data):
        self._account(len(data.encode('utf-8')))
        return super().data(data)

    def end(self, tag):
        element = super().end(tag)
        if self.record_depth == len(self.element_bytes):
            self.record_depth = None
        self.element_bytes.pop()
        return element


class _BoundedXMLReader:
    """Bound Expat's unfinished tokens, including attributes and comments.

    Expat's byte index stays at the beginning of a token it cannot yet parse.
    Measuring that lag also covers encodings whose delimiters are not ASCII.
    """

    def __init__(self, stream, parser):
        self.stream = stream
        self.parser = parser
        self.read_bytes = 0

    def read(self, size=-1):
        pending = self.read_bytes - self.parser.parser.CurrentByteIndex
        allowance = _XML_MAX_BYTES - pending
        if allowance < 0:
            raise ValueError('OOXML unfinished token exceeds 4 MB safety budget')
        count = min(_XML_CHUNK_BYTES, allowance + 1)
        if size >= 0:
            count = min(count, size)
        chunk = self.stream.read(count)
        if pending + len(chunk) > _XML_MAX_BYTES:
            raise ValueError('OOXML unfinished token exceeds 4 MB safety budget')
        self.read_bytes += len(chunk)
        return chunk



def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def records(archive, path: str, names: set[str], *, with_parent: bool = False):
    """Retain only one requested record, removing completed nodes from parents."""
    stack = []
    active = None
    with archive.open(path) as stream:
        parser = DefusedXMLParser(target=_BoundedTreeBuilder(names), forbid_dtd=True)
        reader = _BoundedXMLReader(stream, parser)
        for event, element in iterparse(reader, events=('start', 'end'), parser=parser):
            if event == 'start':
                stack.append(element)
                if active is None and local(element.tag) in names:
                    active = element
                continue
            if element is active:
                parent = stack[-2] if len(stack) > 1 else None
                yield element, parent if with_parent else (local(parent.tag) if parent is not None else '')
                active = None
            if active is None:
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            stack.pop()


def target_path(source: str, target: str) -> str:
    path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(posixpath.dirname(source), target))
    if path.startswith('../') or path == '..':
        raise ValueError('Invalid OOXML relationship target')
    return path


def relationships(archive, source: str, cap: int = 10000):
    source_path = PurePosixPath(source)
    relpath = str(source_path.parent / '_rels' / (source_path.name + '.rels'))
    result = {}
    if relpath not in archive.namelist():
        return result
    if archive.getinfo(relpath).file_size > 8 * 1024 * 1024:
        raise ValueError('OOXML relationships exceed metadata budget')
    for element, _ in records(archive, relpath, {'Relationship'}):
        if len(result) >= cap:
            raise ValueError('Too many OOXML relationships')
        attrs = element.attrib
        target = attrs.get('Target', '')
        if len(target) > 16384:
            raise ValueError('OOXML relationship target exceeds metadata budget')
        external = attrs.get('TargetMode') == 'External'
        result[attrs.get('Id', '')] = {
            'target': target if external else target_path(source, target),
            'type': attrs.get('Type', '').rsplit('/', 1)[-1], 'external': external,
        }
    return result
