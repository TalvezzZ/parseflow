"""真实文件与 ASGI API 集成测试，不依赖第三方云服务。"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from app.main import app


def write_text_pdf(path: Path, text: str) -> None:
    """生成一个使用内置 Helvetica 字体的最小有效文本 PDF。"""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 18 Tf\n72 720 Td\n({escaped}) Tj\nET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    content = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode())
        content.extend(obj)
        content.extend(b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(content)


def write_docx(path: Path, text: str) -> None:
    """生成一个最小但可由 Word/Mammoth 打开的 DOCX 文件。"""
    content_types = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">
  <Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>
  <Default Extension=\"xml\" ContentType=\"application/xml\"/>
  <Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>
</Types>"""
    root_rels = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">
  <Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>
</Relationships>"""
    document = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document)


async def post(path: str, payload: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=payload)


@pytest.mark.asyncio
async def test_real_pdf_parse_api(tmp_path: Path) -> None:
    source = tmp_path / "real-text.pdf"
    write_text_pdf(source, "Real PDF integration test")

    response = await post("/api/v1/parse/pdf", {"file_id": "pdf-real", "path": str(source)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["document"]["document_type"] == "pdf"
    assert "Real PDF integration test" in payload["data"]["document"]["pages"][0]["text"]


@pytest.mark.asyncio
async def test_real_docx_parse_api(tmp_path: Path) -> None:
    source = tmp_path / "real-document.docx"
    write_docx(source, "Real DOCX integration test")

    response = await post("/api/v1/parse/docx", {"file_id": "docx-real", "path": str(source)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    document = payload["data"]["document"]
    assert document["document_type"] == "docx"
    assert "Real DOCX integration test" in document["representations"]["plain_text"]


@pytest.mark.asyncio
async def test_real_docx_to_pdf_conversion_api(tmp_path: Path) -> None:
    if shutil.which("soffice") is None:
        pytest.skip("当前环境未提供 soffice，无法验证真实 LibreOffice 转换")
    source = tmp_path / "real-convert.docx"
    output_dir = tmp_path / "converted"
    write_docx(source, "Real LibreOffice conversion test")

    response = await post(
        "/api/v1/convert/office",
        {
            "file_id": "convert-real",
            "path": str(source),
            "target_format": "pdf",
            "output_dir": str(output_dir),
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    conversion = payload["data"]["conversion"]
    target = Path(conversion["target_path"])
    assert conversion["target_format"] == "pdf"
    assert target.is_file()
    assert target.read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_real_legacy_doc_to_docx_then_parse_api(tmp_path: Path) -> None:
    """用 LibreOffice 生成真实 .doc，再经平台转换回 .docx 并解析。"""
    soffice = shutil.which("soffice")
    if soffice is None:
        pytest.skip("当前环境未提供 soffice，无法验证真实 DOC 回转")

    seed_docx = tmp_path / "legacy-seed.docx"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    write_docx(seed_docx, "Real legacy DOC round-trip test")
    generated = subprocess.run(
        [soffice, "--headless", "--convert-to", "doc", "--outdir", str(legacy_dir), str(seed_docx)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    legacy_doc = legacy_dir / "legacy-seed.doc"
    assert generated.returncode == 0, generated.stderr or generated.stdout
    if not legacy_doc.is_file():
        pytest.skip("当前 LibreOffice 安装未提供 DOC 导出过滤器，跳过真实 DOC 回转验证")

    converted_dir = tmp_path / "converted"
    conversion_response = await post(
        "/api/v1/convert/office",
        {
            "file_id": "legacy-doc",
            "path": str(legacy_doc),
            "target_format": "docx",
            "output_dir": str(converted_dir),
        },
    )

    assert conversion_response.status_code == 200, conversion_response.text
    conversion_payload = conversion_response.json()
    assert conversion_payload["status"] == "success"
    converted_docx = Path(conversion_payload["data"]["conversion"]["target_path"])
    assert converted_docx.is_file()
    assert converted_docx.read_bytes().startswith(b"PK")

    parse_response = await post(
        "/api/v1/parse/docx", {"file_id": "legacy-doc", "path": str(converted_docx)}
    )
    assert parse_response.status_code == 200, parse_response.text
    parse_payload = parse_response.json()
    assert parse_payload["status"] == "success"
    assert "Real legacy DOC round-trip test" in parse_payload["data"]["document"]["representations"]["plain_text"]
