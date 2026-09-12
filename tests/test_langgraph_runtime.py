import json
from types import SimpleNamespace

from app.agent.langgraph_runtime import (
    LangGraphAgentRuntime,
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

    def execute_tool(self, name, arguments):
        self.executed.append((name, arguments))
        return json.dumps({"success": True}, ensure_ascii=False)


def make_runtime(tmp_path, *, manager=None, responses=None):
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
        assert completed["final_text"] == "退款已经提交。"
        assert manager.executed == []
        assert json.loads(completed["tool_history"][0]["output"])["error"] == "not now"
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
