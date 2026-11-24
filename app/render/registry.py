"""渲染器注册表。

新增一种输出表示只需注册一个渲染器，不修改任何 Skill 或 Provider。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.render.contracts import (
    DEFAULT_MEDIA_TYPES,
    RenderArtifact,
    RenderOptions,
    RenderResult,
    normalize_document,
)


@runtime_checkable
class Renderer(Protocol):
    """渲染器接口：只读文档映射，返回内存产物。"""

    kind: str
    media_type: str

    def render(self, document: Mapping[str, Any], options: RenderOptions) -> RenderArtifact:
        """生成一种表示；无法生成时必须抛异常，不得返回空字符串冒充成功。"""


class RenderRegistry:
    """按表示种类注册与查找渲染器。"""

    def __init__(self, renderers: Sequence[Renderer] | None = None) -> None:
        self._renderers: dict[str, Renderer] = {}
        for renderer in renderers or ():
            self.register(renderer)

    def register(self, renderer: Renderer) -> None:
        if renderer.kind in self._renderers:
            raise ValueError(f"渲染器已注册: {renderer.kind}")
        self._renderers[renderer.kind] = renderer

    def get(self, kind: str) -> Renderer:
        try:
            return self._renderers[kind]
        except KeyError as exc:
            raise KeyError(f"未找到渲染器: {kind}") from exc

    def available(self) -> list[str]:
        return list(self._renderers)

    def render(
        self,
        document: Mapping[str, Any] | Any,
        *,
        kinds: Sequence[str] | None = None,
        options: RenderOptions | None = None,
    ) -> RenderResult:
        """渲染请求的表示；缺失的表示记入 missing，不静默跳过。"""
        from app.render.policy import apply_render_policy

        payload = normalize_document(document)
        opts = apply_render_policy(options)
        requested = list(kinds) if kinds is not None else self.available()
        result = RenderResult()
        for kind in requested:
            renderer = self._renderers.get(kind)
            if renderer is None:
                result.missing.append(kind)
                continue
            result.artifacts.append(renderer.render(payload, opts))
        return result


def default_registry() -> RenderRegistry:
    """内置渲染器：markdown、plain_text、json。

    与 1.6.0 行为一致：文档已提供该表示时直接复用，否则按页生成。
    """
    from app.render.json import JsonRenderer
    from app.render.markdown import MarkdownRenderer
    from app.render.text import PlainTextRenderer

    return RenderRegistry([MarkdownRenderer(), PlainTextRenderer(), JsonRenderer()])


def render_representations(
    document: Mapping[str, Any] | Any,
    *,
    kinds: Sequence[str] | None = None,
    options: RenderOptions | None = None,
    registry: RenderRegistry | None = None,
) -> RenderResult:
    """便捷入口：用默认注册表渲染。"""
    return (registry or default_registry()).render(document, kinds=kinds, options=options)


__all__ = [
    "DEFAULT_MEDIA_TYPES",
    "RenderRegistry",
    "Renderer",
    "default_registry",
    "render_representations",
]
