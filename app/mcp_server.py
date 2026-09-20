"""Remote-safe ParseFlow MCP server: only opaque file/task identifiers cross the boundary."""

from pathlib import Path

from app.storage.ids import is_valid_id
from app.version import __version__


def runtime():
    """Import runtime singletons lazily to support standalone stdio and HTTP mounting."""
    from app.main import artifact_repository, file_store, planner, registry, task_manager
    return artifact_repository, file_store, planner, registry, task_manager


try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("未安装 MCP SDK，请执行 uv sync") from exc


mcp = MCPServer("parse-agent", version=__version__)


@mcp.tool()
def list_skills() -> list[dict]:
    """列出可用顶层 Skill 及其能力。"""
    *_, registry, _ = runtime()
    return registry.list_manifests()


@mcp.tool()
def preview_parse_plan(filename_or_suffix: str, goal: str = "", parse_mode: str = "auto") -> dict:
    """仅按展示文件名或后缀预览路由；绝不读取服务端或客户端路径。"""
    _, _, planner, _, _ = runtime()
    candidate = filename_or_suffix.strip()
    if candidate.startswith(".") and "/" not in candidate and "\\" not in candidate:
        candidate = f"preview{candidate}"
    # Path.name deliberately discards any supplied directory portion before planning.
    return planner.create(Path(candidate).name, goal or None, parse_mode).model_dump(mode="json")


@mcp.tool()
async def submit_file_id(file_id: str, goal: str = "", data_id: str = "", parse_mode: str = "auto") -> dict:
    """Submit an already uploaded opaque file ID for asynchronous automatic parsing."""
    _, file_store, _, _, task_manager = runtime()
    if not is_valid_id("file", file_id) or file_store.get(file_id) is None:
        return {"error": {"code": "file_not_found", "message": "未找到文件"}}
    submitted = await task_manager.submit_parse(file_id, goal or None, data_id or None, parse_mode=parse_mode)
    return submitted.model_dump(mode="json")


@mcp.tool()
async def get_task(task_id: str) -> dict:
    """Fetch one persistent task by opaque ID."""
    *_, task_manager = runtime()
    if not is_valid_id("task", task_id):
        return {"error": {"code": "task_not_found", "message": "未找到任务"}}
    task = await task_manager.get(task_id)
    return task.model_dump(mode="json") if task else {"error": {"code": "task_not_found", "message": "未找到任务"}}


@mcp.tool()
async def cancel_task(task_id: str) -> dict:
    """Cancel a queued task or request cooperative cancellation for a running task."""
    *_, task_manager = runtime()
    if not is_valid_id("task", task_id):
        return {"error": {"code": "task_not_found", "message": "未找到任务"}}
    task = await task_manager.cancel(task_id)
    return task.model_dump(mode="json") if task else {"error": {"code": "task_not_found", "message": "未找到任务"}}


@mcp.tool()
async def list_artifacts(task_id: str) -> list[dict]:
    """List task artifacts by opaque artifact IDs; download uses the REST artifact endpoint."""
    artifact_repository, _, _, _, task_manager = runtime()
    if not is_valid_id("task", task_id) or await task_manager.get(task_id) is None:
        return []
    return [item.model_dump(mode="json") | {"download_url": f"/api/v1/tasks/{task_id}/artifacts/{item.artifact_id}"}
            for item in artifact_repository.list(task_id)]


def main() -> None:
    """Run the same opaque-ID-safe MCP profile over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
