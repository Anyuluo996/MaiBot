"""MCP 工具 Provider。"""

from __future__ import annotations

from typing import Optional

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolProvider,
    ToolSpec,
)

from .link_media import enrich_tool_result_with_link_images
from .service import MCPService


class MCPToolProvider(ToolProvider):
    """基于进程级 MCPService 的轻量工具 Provider。"""

    provider_name = "mcp"
    provider_type = "mcp"

    def __init__(self, service: MCPService) -> None:
        """初始化 MCP 工具 Provider。

        Args:
            service: 进程级 MCP 服务。
        """

        self._service = service

    async def list_tools(
        self,
        context: Optional[ToolAvailabilityContext] = None,
    ) -> list[ToolSpec]:
        """列出全部 MCP 工具。"""

        del context
        return await self._service.list_tools()

    async def invoke(
        self,
        invocation: ToolInvocation,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """执行指定 MCP 工具。

        Args:
            invocation: 工具调用请求。
            context: 执行上下文。

        Returns:
            ToolExecutionResult: 工具执行结果。
        """

        result = await self._service.call_tool_invocation(invocation, context)
        # 成功结果中的图片链接下载注册为媒体；失败只跳过注册，不改写结果本身。
        if result.success:
            await enrich_tool_result_with_link_images(result)
        return result

    async def close(self) -> None:
        """Runtime 不拥有共享连接，因此关闭 Provider 时无需释放服务。"""

