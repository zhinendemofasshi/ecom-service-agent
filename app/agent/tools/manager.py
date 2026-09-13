"""ToolManager：统一管理本地工具和 MCP 工具。

当 MCP 启用时，通过 Streamable HTTP 连接 MCP Server 获取工具；
当 MCP 未启用或连接失败时，退回本地工具。
"""

import json
from typing import Optional

from app.agent.tools.registry import TOOL_DEFINITIONS as LOCAL_TOOL_DEFINITIONS
from app.agent.tools.registry import execute_tool as local_execute_tool


class ToolManager:
    """聚合本地工具和 MCP 工具，提供统一的工具定义和调度接口。"""

    def __init__(
        self,
        use_mcp: bool = False,
        mcp_server_url: str = "",
        allowed_tools: Optional[set] = None,
        config=None,
    ):
        from app.config.settings import settings
        self.config = config or settings
        self._mcp_client = None
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []
        self._memory_manager = None
        self._skill_manager = None

        if use_mcp and mcp_server_url:
            self._init_mcp(mcp_server_url)
        else:
            self._init_local()

        if allowed_tools is not None:
            self._filter_tools(allowed_tools)

    def _init_local(self):
        """只加载本地工具。"""
        self._tool_defs = list(LOCAL_TOOL_DEFINITIONS)
        for td in self._tool_defs:
            self._tool_source[td["function"]["name"]] = "local"

    def _init_mcp(self, server_url: str):
        """连接 MCP Server 加载工具；失败时降级到本地工具。"""
        from app.mcp_client import MCPClient

        try:
            self._mcp_client = MCPClient(server_url)
            mcp_tools = self._mcp_client.connect()
            print(f"🔗 [MCP] 已连接 {server_url}，发现 {len(mcp_tools)} 个工具")

            mcp_names = set()
            for td in mcp_tools:
                name = td["function"]["name"]
                mcp_names.add(name)
                self._tool_source[name] = "mcp"
            self._tool_defs = list(mcp_tools)

            for td in LOCAL_TOOL_DEFINITIONS:
                name = td["function"]["name"]
                if name not in mcp_names:
                    self._tool_defs.append(td)
                    self._tool_source[name] = "local"

        except Exception as e:
            print(f"⚠️  [MCP] 连接失败 ({e})，降级使用本地工具")
            if self._mcp_client:
                self._mcp_client.close()
                self._mcp_client = None
            self._init_local()

    def _filter_tools(self, allowed: set):
        """只保留白名单中的工具，用于子 Agent 工具隔离。"""
        self._tool_defs = [
            d for d in self._tool_defs
            if d["function"]["name"] in allowed
        ]
        self._tool_source = {
            k: v for k, v in self._tool_source.items()
            if k in allowed
        }

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_defs

    def execute_tool(self, name: str, arguments: dict, context=None) -> str:
        """根据工具来源分发调用。"""
        source = self._tool_source.get(name)

        if name == "recall_user_memory":
            if self._memory_manager is None or not self._memory_manager.memory_enabled:
                return json.dumps({"success": False, "error_code": "memory_disabled", "error": "记忆系统未启用"}, ensure_ascii=False)
            memory = self._memory_manager
            result = {
                "success": True,
                "short_term_facts": memory.stm.facts,
                "long_term_facts": [
                    {"content": fact.content, "category": fact.category}
                    for fact in memory.ltm.facts
                ],
                "recent_interactions": [
                    item["summary"] for item in memory.ltm.interaction_summaries[-3:]
                ],
            }
            return json.dumps(result, ensure_ascii=False)

        if name == "load_skill":
            if self._skill_manager is None:
                return json.dumps({"success": False, "error_code": "skills_disabled", "error": "技能系统未启用"}, ensure_ascii=False)
            return json.dumps(
                self._skill_manager.load_skill(arguments.get("skill_name", "")),
                ensure_ascii=False,
            )

        if name == "apply_refund":
            return local_execute_tool(name, arguments, context, config=self.config)

        if source == "mcp" and self._mcp_client:
            return self._mcp_client.call_tool(name, arguments)

        if source == "local":
            return local_execute_tool(name, arguments, context, config=self.config)

        return json.dumps({"success": False, "error_code": "unknown_tool", "error": f"未知工具: {name}"}, ensure_ascii=False)

    def bind_agent_context(self, *, memory_manager=None, skill_manager=None) -> None:
        """Bind session-scoped dependencies without process globals."""
        self._memory_manager = memory_manager
        self._skill_manager = skill_manager

    def close(self):
        """清理 MCP 连接。"""
        if self._mcp_client:
            self._mcp_client.close()
            self._mcp_client = None

    def audit_tool_decision(self, name: str, arguments: dict, context, decision: str) -> None:
        if name != "apply_refund":
            return
        from app.agent.tools.refund import record_refund_decision
        settings = self.config
        record_refund_decision(
            db_path=settings.refund_store,
            idempotency_key=context.idempotency_key,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            order_id=str(arguments.get("order_id", "")),
            reason=str(arguments.get("reason", "")),
            decision=decision,
            approver_ids=settings.refund_approver_ids,
            actor_id=context.approved_by,
        )

    def register_tool_approval(self, name, arguments, context, timeout_seconds):
        if name != "apply_refund":
            return
        from app.agent.tools.refund import register_refund_approval
        settings = self.config
        register_refund_approval(
            db_path=settings.refund_store, idempotency_key=context.idempotency_key,
            tenant_id=context.tenant_id, user_id=context.user_id,
            order_id=str(arguments.get("order_id", "")), reason=str(arguments.get("reason", "")),
            timeout_seconds=timeout_seconds,
        )
