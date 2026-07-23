"""
Agent基类
所有Agent的抽象基类，定义统一接口
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime
import asyncio


class AgentResult:
    """Agent执行结果"""

    def __init__(
        self,
        agent_name: str,
        status: str,  # success/failed/running
        output: Dict[str, Any],
        duration_ms: int = 0,
        error: Optional[str] = None
    ):
        self.agent_name = agent_name
        self.status = status
        self.output = output
        self.duration_ms = duration_ms
        self.error = error
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "status": self.status,
            "output": self.output,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "timestamp": self.timestamp
        }


class BaseAgent(ABC):
    """Agent抽象基类"""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.mcp_tools = {}
        self.ai_backend = None

    def set_mcp_tools(self, tools: Dict[str, Any]):
        """设置MCP工具"""
        self.mcp_tools = tools

    def set_ai_backend(self, ai_backend):
        """设置AI后端"""
        self.ai_backend = ai_backend

    @abstractmethod
    async def execute(self, task_context: Dict[str, Any], **kwargs) -> AgentResult:
        """执行Agent任务"""
        pass

    async def _call_ai(self, messages: list, tools: Optional[list] = None) -> Dict[str, Any]:
        """调用AI后端"""
        if not self.ai_backend:
            raise RuntimeError(f"Agent {self.name} 未设置AI后端")

        return await self.ai_backend.chat_completion(
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None
        )

    async def _call_mcp(self, tool_name: str, **params) -> Any:
        """调用MCP工具
        支持两种形式:
        - "regulation_rag.retrieve": 服务名.方法名，调用服务对象上的方法
        - "database_mcp": 直接以注册名调用（要求对象本身可调用）
        """
        if "." in tool_name:
            service_name, method_name = tool_name.split(".", 1)
            service = self.mcp_tools.get(service_name)
            if service is None:
                raise RuntimeError(f"Agent {self.name} 未找到MCP服务: {service_name}")
            method = getattr(service, method_name, None)
            if method is None:
                raise RuntimeError(f"MCP服务 {service_name} 不存在方法: {method_name}")
            return await method(**params)

        if tool_name not in self.mcp_tools:
            raise RuntimeError(f"Agent {self.name} 未找到MCP工具: {tool_name}")

        tool = self.mcp_tools[tool_name]
        return await tool(**params)
