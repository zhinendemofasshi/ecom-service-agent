import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app import observability as obs
from app.model import ModelGateway
from app.agent.tools.executor import ReliableToolExecutor, ToolExecutionContext, ToolPolicy
from tests.test_langgraph_runtime import make_runtime
from tests.test_p1 import Responses


@pytest.fixture
def collector(monkeypatch):
    class Collector:
        def __init__(self):
            self.created, self.updated = [], []
        def create_run(self, **kwargs):
            self.created.append(deepcopy(kwargs))
        def update_run(self, **kwargs):
            self.updated.append(deepcopy(kwargs))
    client = Collector()
    monkeypatch.setattr(obs, "_client", lambda *args: client)
    return client


def config():
    return Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="test-only",
                    langsmith_project="offline-verification")


def test_trace_tree_usage_privacy_and_business_failure(collector):
    response = SimpleNamespace(id="response-secret", usage=SimpleNamespace(
        input_tokens=10, output_tokens=2, total_tokens=12), status="completed")
    gateway = ModelGateway(SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response)))
    manager = SimpleNamespace(tool_definitions=[], execute_tool=lambda *args: '{"success":false,"error":"customer-secret"}')
    executor = ReliableToolExecutor(manager, ToolPolicy(max_retries=0))
    with obs.span("agent.invoke", config=config(), metadata={"request_id":obs.identifier("request-secret")}):
        gateway.create(model="fake", input="customer-secret")
        executor.execute("query_order", {"order_id":"customer-secret"},
                         ToolExecutionContext("tenant", "user", "request-secret", "call-secret"))
    assert len(collector.created) == 4
    root = collector.created[0]
    assert all(r["trace_id"] == root["trace_id"] for r in collector.created)
    llm = next(r for r in collector.updated if r["run_type"] == "llm")
    assert llm["outputs"]["usage_metadata"]["total_tokens"] == 12
    tool = next(r for r in collector.updated if r["name"] == "tool.execute")
    assert tool["error"] == "ToolBusinessError"
    assert tool["outputs"]["success"] is False
    serialized = json.dumps([collector.created, collector.updated], default=str)
    for secret in ("customer-secret", "request-secret", "call-secret", "response-secret", "test-only"):
        assert secret not in serialized


def test_graph_has_explicit_nodes_and_no_state_upload(tmp_path, collector):
    graph, manager = make_runtime(tmp_path, responses=Responses(text="private-answer"))
    manager.config = config()
    try:
        graph.invoke([{"role":"user", "content":"private-question"}], request_id="opaque-request")
    finally:
        graph.close()
    assert {"agent.invoke", "context", "model", "responses.create", "finalize"} <= {
        row["name"] for row in collector.created}
    serialized = json.dumps([collector.created, collector.updated], default=str)
    assert "private-question" not in serialized and "private-answer" not in serialized


def test_export_failure_and_disabled_tracing_do_not_break_model(monkeypatch):
    def unavailable(*args):
        raise ConnectionError("credential-secret")
    monkeypatch.setattr(obs, "_client", unavailable)
    with obs.span("agent", config=config()) as trace:
        trace.outputs["success"] = True
    with obs.span("agent", config=Settings(_env_file=None)):
        pass


def test_exception_messages_are_never_exported(collector):
    with pytest.raises(ValueError):
        with obs.span("agent", config=config()):
            raise ValueError("credential-secret")
    assert collector.updated[-1]["error"] == "ValueError"
    assert "credential-secret" not in json.dumps(collector.updated, default=str)


def test_approval_segments_share_correlation_without_counting_wait_as_run_latency(tmp_path, collector):
    graph, manager = make_runtime(tmp_path)
    manager.config = config()
    try:
        graph.invoke([{"role":"user", "content":"refund"}], request_id="business-request")
        graph.resume([{"type":"approve", "approved_by":"reviewer"}])
        executed = len(manager.executed)
        with pytest.raises(RuntimeError):
            graph.resume([{"type":"approve", "approved_by":"reviewer"}])
        assert len(manager.executed) == executed == 1
    finally:
        graph.close()
    roots = [r for r in collector.updated if r["name"] in {"agent.invoke", "agent.resume"}]
    assert len({r["extra"]["metadata"]["business_run_id"] for r in roots}) == 1
    assert roots[0]["outputs"]["pending_approval"]
    assert roots[1]["extra"]["metadata"]["approval_wait_seconds"] >= 0
    assert not roots[1]["outputs"]["pending_approval"]


def test_concurrent_traces_keep_separate_parents(collector):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    def run(index):
        with obs.span("agent", config=config(), metadata={"request_id":str(index)}):
            barrier.wait(timeout=5)
            with obs.span("model"):
                pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, [1, 2]))
    roots = [r for r in collector.created if r["name"] == "agent"]
    children = [r for r in collector.created if r["name"] == "model"]
    assert len({r["trace_id"] for r in roots}) == 2
    assert {r["parent_run_id"] for r in children} == {r["id"] for r in roots}
