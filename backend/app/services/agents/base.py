from abc import ABC, abstractmethod
from typing import Any


class BaseAgent(ABC):
    """所有项目 Agent 的最小生命周期契约。

    专用 Agent 共享 ``run(input_data, context) -> dict`` 接口，便于路由和
    WorkflowOrchestrator 统一调度；BaseAgent 不负责 LLM 调用、工具循环或输出
    Schema，具体 Agent 自己选择这些实现。
    """

    name: str
    description: str
    available_tools: list[str]

    @abstractmethod
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def validate_output(self, output: dict[str, Any]) -> dict[str, Any]:
        # 当前三个专用 Agent 在 run 内直接调用各自的 Pydantic Output model；
        # 这个通用钩子保留给需要统一后处理的其他 Agent。
        return output

    def handle_error(self, error: Exception, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "error": str(error),
            "agent": self.name,
            "context": {"repo_id": str(context.get("repo_id")) if context.get("repo_id") else None},
        }
