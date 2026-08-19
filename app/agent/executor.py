from app.documents.models import ParseContext, SkillResult
from app.skills.registry import SkillRegistry


class SkillExecutor:
    """按计划执行已注册 Skill，暂不包含重试和并行策略。"""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    async def execute(self, skill_name: str, context: ParseContext) -> SkillResult:
        skill = self.registry.get(skill_name)
        return await skill.execute(context)

