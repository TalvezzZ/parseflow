from abc import ABC, abstractmethod
from typing import Any

from app.documents.models import ParseContext, SkillResult


class SkillManifest(dict):
    """Skill 能力描述，暂时使用轻量字典，后续可升级为 Pydantic 模型。"""


class Skill(ABC):
    """所有解析和知识加工能力的统一接口。"""

    name: str
    version: str = "0.1.0"

    @property
    @abstractmethod
    def manifest(self) -> SkillManifest:
        """返回 Skill 的能力声明。"""

    @abstractmethod
    async def execute(self, context: ParseContext) -> SkillResult:
        """执行 Skill。"""


class EchoSkill(Skill):
    """用于验证 Agent 骨架的示例 Skill。"""

    name = "system.echo"

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name=self.name,
            version=self.version,
            kind="utility",
            input_types=["*"],
            capabilities=["pipeline_smoke_test"],
        )

    async def execute(self, context: ParseContext) -> SkillResult:
        return SkillResult(
            status="success",
            skill_name=self.name,
            data={"file_id": context.file.file_id, "goal": context.goal},
        )
