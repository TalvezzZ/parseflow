"""阶段 0 门禁验证：能否用外部构造的 MiddleJson 渲染出 7 种格式。

不读任何源文档，只用手工构造的 IR，验证 v1.6.1 方案中的技术赌点。
结论记录在 docs/docvortex-adoption.md 第 6 节。

本脚本不属于产品依赖，需在独立环境安装 docvortex 后运行：

    python -m venv /tmp/dv-spike
    /tmp/dv-spike/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple docvortex
    /tmp/dv-spike/bin/python scripts/spike_docvortex_render.py

IR 约束（由本次验证实测得到，接入时必须满足）：
- MiddleJson 需要 is_full_document，顶层块必须带 index；
- 子块 index 必须等于父块 index（如 table_body 与 table）；
- file_suffix="pdf" 时顶层块必须带归一化 bbox（0..1），否则校验失败；
- 图片必须用 `data:image/<type>;base64,...` 或相对 sidecar 路径，绝对路径被拒绝。
"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from pathlib import Path

from docvortex import schema
from docvortex.render import RenderFormat, render

OUT = Path("/tmp/spike-out")
BBOX = (0.08, 0.08, 0.92, 0.16)


def png_data_uri() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 32), (40, 90, 160)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_middle(image_uri: str) -> schema.MiddleJson:
    """模拟 DocumentResult -> MiddleJson 的映射产物。"""
    meta = schema.DocumentMetadata(
        file_suffix="pdf",
        producer=schema.Producer(name="parseflow-spike", version="0.0.1"),
        document=schema.DocumentProperties(title="阶段 0 验证文档"),
    )
    blocks = [
        schema.DocTitleBlock(
            type="doc_title",
            index=0,
            bbox=BBOX,
            level=1,
            content=[schema.TextSpan(type="text", content="阶段 0 验证文档")],
        ),
        schema.ParagraphTitleBlock(
            type="paragraph_title",
            index=1,
            bbox=BBOX,
            level=2,
            content=[schema.TextSpan(type="text", content="1. 背景")],
        ),
        schema.TextBlock(
            type="text",
            index=2,
            bbox=BBOX,
            content=[
                schema.TextSpan(type="text", content="这一段用于验证"),
                schema.TextSpan(type="text", content="加粗", styles=["bold"]),
                schema.TextSpan(type="text", content="与行内公式 "),
                schema.EquationInlineSpan(type="equation_inline", content="E = mc^2"),
                schema.TextSpan(type="text", content=" 的渲染结果。"),
            ],
        ),
        schema.EquationBlock(type="equation", index=3, bbox=BBOX, content=r"\int_0^1 x^2 \, dx = \frac{1}{3}"),
        schema.TableBlock(
            type="table",
            index=4,
            bbox=BBOX,
            content=[
                schema.TableBodyBlock(
                    type="table_body",
                    index=4,
                    content=(
                        "<table><thead><tr><th>指标</th><th>数值</th></tr></thead>"
                        "<tbody><tr><td>耗时</td><td>14.48 s</td></tr>"
                        "<tr><td>峰值 RSS</td><td>降低 31%</td></tr></tbody></table>"
                    ),
                )
            ],
        ),
        schema.ListBlock(
            type="list",
            index=5,
            bbox=BBOX,
            content=[
                schema.TextBlock(type="text", index=5, bbox=BBOX, content=[schema.TextSpan(type="text", content="第一项")]),
                schema.TextBlock(type="text", index=5, bbox=BBOX, content=[schema.TextSpan(type="text", content="第二项")]),
            ],
        ),
        schema.ImageBlock(
            type="image",
            index=6,
            bbox=BBOX,
            content=[schema.ImageBodyBlock(type="image_body", index=6, content="", image_base64=image_uri)],
        ),
    ]
    return schema.MiddleJson(
        metadata=meta,
        pages=[schema.PageInfo(page_idx=0, blocks=blocks)],
        is_full_document=True,
    )


def payload_of(output: object) -> bytes:
    """render() 对不同格式返回 str/bytes/dict 或带 .data 的 RenderOutput。"""
    if isinstance(output, bytes):
        return output
    if isinstance(output, str):
        return output.encode()
    if isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False).encode()
    data = getattr(output, "data", None)
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode()
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False).encode()
    return str(output).encode()


def check(fmt: str, payload: bytes) -> dict:
    """按格式做结构抽查，而不是只看是否抛异常。"""
    info: dict[str, object] = {"bytes": len(payload)}
    if fmt == "markdown":
        text = payload.decode("utf-8", "replace")
        info.update(table="|" in text, heading="#" in text, image="![" in text or "<img" in text)
    elif fmt == "html":
        text = payload.decode("utf-8", "replace")
        # HTML 输出用 LaTeX 定界符 + docvortex-math 类（供 MathJax 客户端渲染），不是服务端 MathML。
        info.update(table="<table" in text, math="docvortex-math" in text, img="<img" in text)
    elif fmt == "latex":
        text = payload.decode("utf-8", "replace")
        # 表格用 longtable + \cellcolor（需要 colortbl 宏包）。
        info.update(doc="\\begin{document}" in text, table="longtable" in text, math="frac" in text)
    elif fmt == "docx":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
            names = zf.namelist()
        info.update(omml="m:oMath" in xml, table="<w:tbl>" in xml, media=any(n.startswith("word/media/") for n in names))
    elif fmt == "epub":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            mimetype = zf.read("mimetype").decode().strip() if "mimetype" in names else ""
        info.update(mimetype=mimetype, nav=any("nav" in n or "toc" in n.lower() for n in names))
    elif fmt == "pdf":
        info.update(header=payload[:5] == b"%PDF-")
    elif fmt == "structured_content":
        data = json.loads(payload.decode("utf-8"))
        info.update(keys=sorted(data)[:6])
    return info


def main() -> int:
    OUT.mkdir(exist_ok=True)
    middle = build_middle(png_data_uri())

    as_dict = middle.to_dict()
    restored = schema.MiddleJson.from_dict(as_dict)
    print(f"IR 往返一致: {restored == middle}; JSON 字节: {len(json.dumps(as_dict))}")

    results: dict[str, dict] = {}
    ok = 0
    for fmt in RenderFormat:
        started = time.perf_counter()
        try:
            payload = payload_of(render(middle, fmt))
            elapsed = (time.perf_counter() - started) * 1000
            suffix = {"markdown": "md", "structured_content": "json"}.get(fmt.value, fmt.value)
            (OUT / f"out.{suffix}").write_bytes(payload)
            info = check(fmt.value, payload)
            info["ms"] = round(elapsed, 1)
            results[fmt.value] = {"status": "ok", **info}
            ok += 1
            print(f"[OK]   {fmt.value:20s} {len(payload):8d} B {elapsed:7.1f} ms {info}")
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - started) * 1000
            results[fmt.value] = {"status": "fail", "error": f"{type(exc).__name__}: {exc}", "ms": round(elapsed, 1)}
            print(f"[FAIL] {fmt.value:20s} {type(exc).__name__}: {exc}")

    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n成功 {ok}/{len(list(RenderFormat))}")
    return 0 if ok >= 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
