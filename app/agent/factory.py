from typing import Any

from app.config import get_settings
from app.documents.models import FileInput, ParseContext
from app.agent.executor import SkillExecutor
from app.skills.registry import SkillRegistry


def create_parse_agent(registry: SkillRegistry) -> Any:
    """创建 LangChain Agent；模型配置缺失时明确报错。"""

    settings = get_settings()
    if not settings.openai_api_key or not settings.openai_model:
        raise RuntimeError("创建 Agent 前请配置 OPENAI_API_KEY 和 OPENAI_MODEL")

    from langchain.agents import create_agent
    from langchain_openai import ChatOpenAI

    model_kwargs = {"model": settings.openai_model, "api_key": settings.openai_api_key}
    if settings.openai_base_url:
        model_kwargs["base_url"] = settings.openai_base_url
    model = ChatOpenAI(**model_kwargs)

    executor = SkillExecutor(registry)

    from langchain_core.tools import tool

    @tool
    def list_skills() -> list[dict]:
        """列出当前可以调用的顶层 Skill。"""

        return registry.list_manifests()

    @tool
    async def execute_skill(
        skill_name: str,
        file_id: str,
        path: str,
        filename: str = "",
        mime_type: str = "",
    ) -> dict:
        """执行一个已注册的顶层 Skill，不要直接调用底层 Provider。"""

        context = ParseContext(
            file=FileInput(
                file_id=file_id,
                path=path,
                filename=filename or None,
                mime_type=mime_type or None,
            )
        )
        result = await executor.execute(skill_name, context)
        return result.model_dump()

    system_prompt = (
        "你是文档解析规划 Agent。只能选择 Skill 清单中的能力，先解释选择依据，"
        "再输出结构化解析计划。不要直接伪造解析结果。\n"
        f"当前 Skill 清单：{registry.list_manifests()}"
    )
    return create_agent(model=model, tools=[list_skills, execute_skill], system_prompt=system_prompt)
