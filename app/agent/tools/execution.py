"""ReAct 工具执行辅助：同一轮内安全复用幂等只读调用。"""

from __future__ import annotations

import json


READ_ONLY_TOOLS = frozenset(
    {
        "query_order",
        "query_product",
        "query_logistics",
        "search_knowledge",
        "list_user_orders",
        "recall_user_memory",
        "load_skill",
    }
)


def tool_cache_key(name: str, arguments: dict) -> str | None:
    """Return a stable key for cacheable calls; writes are never cacheable."""
    if name not in READ_ONLY_TOOLS:
        return None
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{name}:{serialized}"
