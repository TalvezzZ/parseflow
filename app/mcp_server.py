"""Parse Agent MCP Server，默认通过 stdio 提供工具。"""

from app.documents.models import FileInput, OfficePipelineRequest, ParseContext
from app.main import executor, pipeline, planner, task_manager, registry

try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("未安装 MCP SDK，请执行 uv sync") from exc


mcp = MCPServer("parse-agent", version="0.6.0")


@mcp.tool()
def list_skills() -> list[dict]:
    """列出可用的顶层 Skill 及其能力。"""
    return registry.list_manifests()


@mcp.tool()
async def execute_skill(skill_name: str, file_id: str, path: str, filename: str = "", mime_type: str = "") -> dict:
    """执行一个顶层 Skill；不会直接暴露底层 Provider。"""
    context = ParseContext(file=FileInput(file_id=file_id, path=path, filename=filename or None, mime_type=mime_type or None))
    result = await executor.execute(skill_name, context)
    return result.model_dump()


@mcp.tool()
def preview_parse_plan(path: str, goal: str = "") -> dict:
    """预览规则计划，不执行文件解析；调用方可据此理解自动路由。"""
    return planner.create(path, goal or None).model_dump(mode="json")


@mcp.tool()
async def submit_parse_intent(file_id: str, path: str, goal: str = "", data_id: str = "", callback: str = "") -> dict:
    """提交自动规划任务；MCP 客户端提供可访问的本地文件路径。"""
    request = {"file_id": file_id, "path": path, "goal": goal or None}
    submitted = await task_manager.submit("parse.intent", request, data_id or None, callback or None)
    return submitted.model_dump(mode="json")


@mcp.tool()
async def parse_office_pipeline(
    file_id: str,
    path: str,
    filename: str = "",
    mime_type: str = "",
    output_dir: str = "",
    timeout_seconds: int = 300,
) -> dict:
    """将旧版 Office 转换为现代格式并自动调用对应解析 Skill。"""
    request = OfficePipelineRequest(
        file_id=file_id,
        path=path,
        filename=filename or None,
        mime_type=mime_type or None,
        output_dir=output_dir or None,
        timeout_seconds=timeout_seconds,
    )
    result = await pipeline.execute(request.to_context())
    return result.model_dump()


def main() -> None:
    """以 stdio 方式启动 MCP Server。"""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
