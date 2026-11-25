"""渲染层数据契约。

渲染输入统一使用归一化后的文档映射（`DocumentResult.model_dump()` 的形状），
避免渲染器与 pydantic 模型或具体 Provider 耦合。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

RenderKind = Literal["markdown", "plain_text", "json"]

# 每种表示默认的媒体类型；未知表示由渲染器自行声明。
DEFAULT_MEDIA_TYPES: dict[str, str] = {
    "markdown": "text/markdown; charset=utf-8",
    "plain_text": "text/plain; charset=utf-8",
    "json": "application/json",
}


class RenderOptions(BaseModel):
    """渲染策略。

    默认值必须复现 1.6.0 的既有输出，否则 v1.6.1 的字节等价验收不成立。
    """

    prefer_document_representation: bool = Field(
        default=True,
        description="文档已提供该表示时直接复用，不再从页面重新生成",
    )
    page_heading_template: str = Field(
        default="## 第 {page} 页",
        description="分页来源的页标题模板；仅在没有既有表示时使用",
    )
    page_separator: str = Field(default="\n\n---\n\n", description="分页来源的页间分隔符")
    page_block_separator: str = Field(default="\n\n", description="页内文本/块拼接分隔符")
    body_source: Literal["text", "blocks", "sheet"] = Field(
        default="text",
        description="正文来源：text 用 page.text；blocks 用页内块文本（幻灯片）；sheet 按块元数据分组（工作簿）",
    )
    plain_text_skip_empty: bool = Field(
        default=True,
        description="纯文本是否跳过空页；PDF 逐页来源跳过，幻灯片来源不跳过",
    )
    plain_text_block_separator: str = Field(
        default="\n",
        description="纯文本在无页面、仅有块时（如工作簿）的块拼接分隔符",
    )
    page_asset_links: bool = Field(
        default=False,
        description="是否在每页正文后追加该页图片引用（如 OCR 版面裁剪图）",
    )
    asset_link_template: str = Field(
        default="![第 {page} 页图片]({path})",
        description="图片引用模板；{page} 为页码，{path} 为相对路径",
    )
    asset_link_separator: str = Field(default="\n\n", description="正文与图片引用、引用之间的分隔符")
    sheet_title_template: str = Field(default="## {name}", description="工作簿分组标题模板")
    sheet_row_template: str = Field(default="- {row}", description="工作簿每行模板")
    sheet_cell_separator: str = Field(default=" | ", description="工作簿单元格分隔符")
    sheet_skip_empty: bool = Field(default=True, description="无文本工作表是否跳过")
    include_empty_pages: bool = Field(
        default=True,
        description="无文本页面是否仍输出页标题",
    )


class RenderArtifact(BaseModel):
    """一种表示的内存产物，尚未落盘。"""

    kind: str
    media_type: str
    text: str
    renderer: str
    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


class RenderRequest(BaseModel):
    """一次渲染请求：对哪些表示、用什么策略。"""

    document: dict[str, Any]
    kinds: list[str] | None = Field(
        default=None,
        description="要生成的表示；None 表示按注册顺序生成全部可用表示",
    )
    options: RenderOptions = Field(default_factory=RenderOptions)


class RenderResult(BaseModel):
    """渲染结果集合。"""

    artifacts: list[RenderArtifact] = Field(default_factory=list)
    missing: list[str] = Field(
        default_factory=list,
        description="请求了但没有渲染器或无法生成的表示，必须显式记录",
    )

    def get(self, kind: str) -> RenderArtifact | None:
        for artifact in self.artifacts:
            if artifact.kind == kind:
                return artifact
        return None

    def as_representations(self) -> dict[str, str]:
        return {artifact.kind: artifact.text for artifact in self.artifacts}


def normalize_document(document: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """把 DocumentResult 或已序列化映射统一成渲染层使用的字典。"""
    if isinstance(document, BaseModel):
        return document.model_dump()
    return dict(document)
