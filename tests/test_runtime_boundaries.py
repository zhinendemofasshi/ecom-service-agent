import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.agent.tools.manager import ToolManager
from app.config.settings import Settings
from app.model import ModelGateway
from tests.test_langgraph_runtime import FakeOutputItem, FakeToolManager, make_runtime
from tests.test_p1 import Responses


def test_step_budget_stops_repeated_tool_calls(tmp_path):
    class Loop:
        def __init__(self):
            self.requests = []
        def create(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(id="r", output_text="", output=[
                FakeOutputItem(item_type="function_call", call_id=str(len(self.requests)),
                               name="apply_refund", arguments='{"order_id":"o","reason":"r"}')])
    responses = Loop()
    graph, manager = make_runtime(tmp_path, responses=responses)
    graph.max_steps = 2
    graph.hitl_enabled = False
    try:
        graph.invoke([{"role": "user", "content": "test"}])
        assert len(responses.requests) == 2
        assert len(manager.executed) == 1
        assert "tools" not in responses.requests[-1]
    finally:
        graph.close()


@pytest.mark.parametrize("arguments", ['{bad', '[]', '{"order_id":12}'])
def test_invalid_tool_arguments_never_execute(tmp_path, arguments):
    responses = Responses([FakeOutputItem(item_type="function_call", call_id="x",
                                         name="apply_refund", arguments=arguments)])
    graph, manager = make_runtime(tmp_path, responses=responses)
    from app.agent.guardrails import Guardrails
    graph.guards = Guardrails()
    try:
        result = graph.invoke([{"role": "user", "content": "查询"}])
        assert not manager.executed
        assert not result.get("__interrupt__")
        assert json.loads(result["tool_history"][0]["output"])["success"] is False
    finally:
        graph.close()


def test_corrupted_checkpoint_fails_without_model_or_tool_execution(tmp_path):
    graph, manager = make_runtime(tmp_path, responses=Responses())
    graph.invoke([{"role": "user", "content": "你好"}])
    graph.close()
    with sqlite3.connect(tmp_path / "checkpoints.sqlite") as db:
        db.execute("UPDATE checkpoints SET checkpoint = ?", (b"corrupted",))
    responses = Responses()
    restored, _ = make_runtime(tmp_path, manager=manager, responses=responses)
    try:
        with pytest.raises(Exception):
            restored.invoke([{"role": "user", "content": "继续"}])
        assert not responses.requested
        assert not manager.executed
    finally:
        restored.close()


def test_filtered_special_tools_cannot_bypass_dispatch():
    manager = ToolManager(config=Settings(_env_file=None), allowed_tools={"query_product"})
    for name in ("apply_refund", "load_skill", "recall_user_memory", "list_user_orders", "shell"):
        assert json.loads(manager.execute_tool(name, {}))["error_code"] == "policy_denied"


def test_mcp_requires_trust_and_filters_discovered_tools(monkeypatch):
    from app.agent.tools.registry import TOOL_DEFINITIONS
    class MCP:
        def __init__(self, url): pass
        def connect(self):
            return [*TOOL_DEFINITIONS, {"type":"function", "function":{
                "name":"shell", "parameters":{"type":"object"}}}]
        def call_tool(self, name, args):
            return '{"success":true}'
        def close(self): pass
    monkeypatch.setattr("app.mcp_client.MCPClient", MCP)
    with pytest.raises(ValueError, match="trusted"):
        ToolManager(use_mcp=True, mcp_server_url="https://untrusted", config=Settings(_env_file=None))
    cfg = Settings(_env_file=None, mcp_trusted_server_urls=["http://mock"])
    with pytest.raises(ValueError, match="allowlist"):
        ToolManager(use_mcp=True, mcp_server_url="http://mock", config=cfg)
    cfg.mcp_allowed_tools = ["query_product"]
    manager = ToolManager(use_mcp=True, mcp_server_url="http://mock", config=cfg)
    assert "shell" not in {d["function"]["name"] for d in manager.tool_definitions}
    assert json.loads(manager.execute_tool("shell", {}))["success"] is False
    assert manager._tool_source["query_order"] == "local"
    assert manager._tool_source["query_product"] == "mcp"


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
def test_responses_failure_status_in_stream_and_nonstream(status):
    from contextlib import contextmanager
    response = SimpleNamespace(status=status)
    @contextmanager
    def stream(**kwargs):
        yield SimpleNamespace(get_final_response=lambda: response)
    gateway = ModelGateway(SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kw: response, stream=stream)))
    with pytest.raises(RuntimeError):
        gateway.create(model="fake")
    with pytest.raises(RuntimeError):
        with gateway.stream(model="fake") as result:
            result.get_final_response()
