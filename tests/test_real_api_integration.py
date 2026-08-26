"""Real PDF/DOCX public upload-task integration tests."""
from __future__ import annotations

import asyncio
from pathlib import Path
import httpx
import pytest
from app.main import app


def write_text_pdf(path: Path, text: str) -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 18 Tf\n72 720 Td\n({escaped}) Tj\nET\n".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content)); content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content); content.extend(f"xref\n0 {len(objects)+1}\n".encode() + b"0000000000 65535 f \n")
    for offset in offsets[1:]: content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(content)


async def parse_upload(path: Path, media_type: str) -> dict:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        created = await client.post("/api/v1/tasks/parse", files={"file": (path.name, path.read_bytes(), media_type)})
        assert created.status_code == 202, created.text
        for _ in range(150):
            task = (await client.get(f"/api/v1/tasks/{created.json()['task_id']}")).json()
            if task["status"] in {"succeeded", "partial", "failed", "cancelled", "interrupted"}: return task
            await asyncio.sleep(.02)
    raise AssertionError("task did not finish")


@pytest.mark.asyncio
async def test_real_pdf_parse_upload_task(tmp_path: Path) -> None:
    source = tmp_path / "real.pdf"; write_text_pdf(source, "Real PDF integration test")
    task = await parse_upload(source, "application/pdf")
    assert task["status"] == "succeeded"
    assert "Real PDF integration test" in task["result"]["document"]["representations"]["plain_text"]


@pytest.mark.asyncio
async def test_real_docx_parse_upload_task(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    source = tmp_path / "real.docx"; document = docx.Document(); document.add_paragraph("Real DOCX integration test"); document.save(source)
    task = await parse_upload(source, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert task["status"] == "succeeded"
    assert "Real DOCX integration test" in task["result"]["document"]["representations"]["plain_text"]


@pytest.mark.asyncio
async def test_legacy_direct_conversion_route_is_removed() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/api/v1/convert/office", json={"path": "/tmp/private.docx", "output_dir": "/tmp/out"})
    assert response.status_code == 404
