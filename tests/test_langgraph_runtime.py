import json
from types import SimpleNamespace

from app.agent.langgraph_runtime import (
    LangGraphAgentRuntime,
    RunCancelled,
    messages_to_responses_input,
    responses_tool_definitions,
)


class FakeOutputItem:
    def __init__(self, *, item_type, call_id=None, name=None, arguments=None, text=None):
        self.type = item_type
        self.call_id = call_id
        self.name = name
        self.arguments = arguments
        self.text = text

    def model_dump(self, exclude_none=True):
        if self.type == "function_call":
            return {
                "type": self.type,
                "call_id": self.call_id,
                "name": self.name,
                "arguments": self.arguments,
            }
        return {"type": "message", "role": "assistant", "content": self.text or ""}


class FakeResponses:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            item = FakeOutputItem(
                item_type="function_call",
                call_id="refund-1",
                name="apply_refund",
                arguments=json.dumps({"order_id": "ORD-1", "reason": "质量问题"}),
            )
            return SimpleNamespace(id="resp-1", output=[item], output_text="")
        item = FakeOutputItem(item_type="message", text="退款已经提交。")
        return SimpleNamespace(
            id="resp-2", output=[item], output_text="退款已经提交。"
        )


class FakeToolManager:
    def __init__(self):
        self.executed = []
        self.tool_definitions = [{
            "type": "function",
            "function": {
                "name": "apply_refund",
                "description": "申请退款",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["order_id", "reason"],
                },
            },
        }]

    def execute_tool(self, name, arguments, context=None):
        self.executed.append((name, arguments))
        return json.dumps({"success": True}, ensure_ascii=False)


def make_runtime(tmp_path, *, manager=None, responses=None, event_sink=None):
    manager = manager or FakeToolManager()
    client = SimpleNamespace(responses=responses or FakeResponses())
    runtime = LangGraphAgentRuntime(
        client=client,
        model="fake-model",
        temperature=0.0,
        tool_manager=manager,
        max_steps=5,
        checkpoint_path=str(tmp_path / "checkpoints.sqlite"),
        thread_key="test-thread",
        hitl_enabled=True,
        event_sink=event_sink,
    )
    return runtime, manager


def test_schema_and_message_conversion():
    definitions = FakeToolManager().tool_definitions
    converted = responses_tool_definitions(definitions)
    assert converted[0]["name"] == "apply_refund"
    assert "function" not in converted[0]

    instructions, items = messages_to_responses_input([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ])
    assert instructions == "system"
    assert items == [{"role": "user", "content": "hello"}]


def test_sensitive_tool_interrupts_and_resumes(tmp_path):
    runtime, manager = make_runtime(tmp_path)
    try:
        result = runtime.invoke([
            {"role": "system", "content": "help"},
            {"role": "user", "content": "refund"},
        ])
        assert result["__interrupt__"]
        assert runtime.has_pending_interrupt()
        assert manager.executed == []

        completed = runtime.resume([{"type": "approve"}])
        assert completed["final_text"] == "退款已经提交。"
        assert manager.executed == [
            ("apply_refund", {"order_id": "ORD-1", "reason": "质量问题"})
        ]
        assert completed["tool_history"][0]["name"] == "apply_refund"
        assert not runtime.has_pending_interrupt()
    finally:
        runtime.close()


def test_sensitive_tool_can_be_rejected(tmp_path):
    runtime, manager = make_runtime(tmp_path)
    try:
        runtime.invoke([{"role": "user", "content": "refund"}])
        completed = runtime.resume([{"type": "reject", "message": "not now"}])
        assert "not now" in completed["final_text"]
        assert manager.executed == []
        assert json.loads(completed["tool_history"][0]["output"])["error"] == "not now"
    finally:
        runtime.close()


def test_expired_approval_fails_closed(tmp_path):
    runtime, manager = make_runtime(tmp_path)
    runtime.approval_timeout_seconds = -1
    try:
        runtime.invoke([{"role": "user", "content": "refund"}])
        completed = runtime.resume([{"type": "approve", "approved_by": "reviewer"}])
        assert manager.executed == []
        output = json.loads(completed["tool_history"][0]["output"])
        assert "超时" in output["error"]
    finally:
        runtime.close()


def test_interrupt_survives_runtime_restart(tmp_path):
    manager = FakeToolManager()
    responses = FakeResponses()
    runtime, _ = make_runtime(tmp_path, manager=manager, responses=responses)
    runtime.invoke([{"role": "user", "content": "refund"}])
    runtime.close()

    restored, _ = make_runtime(tmp_path, manager=manager, responses=responses)
    try:
        assert restored.has_pending_interrupt()
        completed = restored.resume([{"type": "approve"}])
        assert completed["final_text"] == "退款已经提交。"
        assert len(manager.executed) == 1
    finally:
        restored.close()


def test_responses_token_stream_is_emitted(tmp_path):
    final = SimpleNamespace(
        id="resp-final",
        output=[FakeOutputItem(item_type="message", text="你好")],
        output_text="你好",
    )

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="你")
            yield SimpleNamespace(type="response.output_text.delta", delta="好")

        def get_final_response(self):
            return final

    class StreamingResponses:
        def stream(self, **kwargs):
            return FakeStream()

    events = []
    runtime, _ = make_runtime(
        tmp_path,
        responses=StreamingResponses(),
        event_sink=events.append,
    )
    try:
        completed = runtime.invoke([{"role": "user", "content": "hello"}], request_id="req-1")
        assert completed["final_text"] == "你好"
        assert [event["delta"] for event in events if event["type"] == "token_delta"] == ["你", "好"]
        assert events[0] == {"type": "run_started", "request_id": "req-1"}
    finally:
        runtime.close()


def test_stream_can_be_cancelled_at_token_boundary(tmp_path):
    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="first")
            yield SimpleNamespace(type="response.output_text.delta", delta="second")

        def get_final_response(self):
            raise AssertionError("cancelled stream must not complete")

    class StreamingResponses:
        def stream(self, **kwargs):
            return FakeStream()

    events = []
    runtime, _ = make_runtime(
        tmp_path, responses=StreamingResponses(), event_sink=events.append
    )
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 3

    try:
        try:
            runtime.invoke(
                [{"role": "user", "content": "hello"}],
                request_id="req-cancel",
                cancel_check=cancelled,
            )
            assert False, "expected cancellation"
        except RunCancelled:
            pass
        assert any(event["type"] == "run_cancelled" for event in events)
    finally:
        runtime.close()


def test_real_refund_service_graph_approval_and_cancel(tmp_path):
    import sqlite3
    from app.agent.tools.manager import ToolManager
    from app.config.settings import Settings

    class OrderResponses(FakeResponses):
        def create(self, **kwargs):
            result = super().create(**kwargs)
            if result.output[0].type == "function_call":
                result.output[0].arguments = json.dumps({"order_id": "ORD-20240115-001", "reason": "质量问题"})
            return result

    for action in ("approve", "cancel"):
        db = str(tmp_path / f"{action}-refunds.sqlite")
        config = Settings(_env_file=None, refund_db_path=db, refund_approver_ids=["reviewer"])
        responses = OrderResponses()
        runtime = LangGraphAgentRuntime(
            client=SimpleNamespace(responses=responses), model="fake", temperature=0,
            tool_manager=ToolManager(config=config), max_steps=5,
            checkpoint_path=str(tmp_path / f"{action}-checkpoint.sqlite"),
            thread_key=action, hitl_enabled=True, tenant_id="tenant-a", user_id="user-xiaoming",
        )
        try:
            runtime.invoke([{"role": "user", "content": "refund"}])
            if action == "cancel":
                runtime.cancel_pending("user-xiaoming")
                assert responses.calls == 1  # cancellation must not call the model
                assert not runtime.has_pending_interrupt()
            else:
                result = runtime.resume([{"type": "approve", "approved_by": "reviewer"}])
                assert json.loads(result["tool_history"][0]["output"])["success"]
            with sqlite3.connect(db) as connection:
                assert connection.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == (1 if action == "approve" else 0)
                assert connection.execute("SELECT status FROM refund_approvals").fetchone()[0] == ("consumed" if action == "approve" else "reject")
        finally:
            runtime.close()
