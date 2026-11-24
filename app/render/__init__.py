"""渲染层：把统一文档结果转成对外表示。

分层约定（见 docs/roadmap.md 5.6 与 docs/plan-1.6.1.md）：

    解析（Skill/Provider） → DocumentResult → render（内存产物） → export（落盘/预算/原子发布）

- 渲染只读文档结果，不持有 Skill、Provider 或文件句柄，也不写磁盘。
- 字节预算、暂存目录、原子发布、可杀 worker 属于导出层，不在本包内。
"""

from app.render.contracts import (
    RenderArtifact,
    RenderOptions,
    RenderRequest,
    RenderResult,
)
from app.render.registry import RenderRegistry, Renderer, default_registry, render_representations

__all__ = [
    "RenderArtifact",
    "RenderOptions",
    "RenderRequest",
    "RenderResult",
    "RenderRegistry",
    "Renderer",
    "default_registry",
    "render_representations",
]
