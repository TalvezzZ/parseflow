"""渲染策略：把全局配置叠加到调用方选项上。

调用方只表达"这次渲染要什么"，全局开关（如中文空格清理）在这里统一注入，
避免每个 Provider 各自读取配置导致行为不一致。
"""

from __future__ import annotations

from app.render.contracts import RenderOptions


def apply_render_policy(options: RenderOptions | None = None) -> RenderOptions:
    """叠加全局渲染策略；调用方显式开启的选项优先，不会被关闭。"""
    from app.config import get_settings

    base = options or RenderOptions()
    if base.cjk_generated_space_removal:
        return base
    try:
        enabled = bool(get_settings().render_cjk_space_removal)
    except Exception:  # 配置不可用时不得影响渲染主链路
        enabled = False
    return base if not enabled else base.model_copy(update={"cjk_generated_space_removal": True})
