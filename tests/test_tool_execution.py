from app.agent.tools.execution import tool_cache_key


def test_read_only_tool_cache_key_is_stable() -> None:
    first = tool_cache_key("query_order", {"order_id": "ORD-1", "verbose": True})
    second = tool_cache_key("query_order", {"verbose": True, "order_id": "ORD-1"})

    assert first == second


def test_write_tool_is_never_cacheable() -> None:
    assert tool_cache_key("apply_refund", {"order_id": "ORD-1"}) is None
