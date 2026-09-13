"""LangGraph execution runtime backed by the OpenAI Responses API.

The graph owns orchestration and durable interruption. Business tools remain in
ToolManager, so authorization, idempotency, and transactions stay outside the
model-facing runtime.
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.checkpointing import CheckpointStore
from app.agent.tools.execution import tool_cache_key
from app.agent.tools.executor import ReliableToolExecutor, ToolExecutionContext, ToolPolicy


SENSITIVE_TOOLS = {"apply_refund"}


class RunCancelled(RuntimeError):
    pass


class AgentGraphState(TypedDict, total=False):
    instructions: str
    input_items: list[dict[str, Any]]
    response_items: list[dict[str, Any]]
    pending_calls: list[dict[str, Any]]
    final_text: str
    steps: int
    events: list[dict[str, Any]]
    tool_history: list[dict[str, Any]]
    execution_context: dict[str, str]
    approval_requested_at: float


def responses_tool_definitions(definitions: list[dict]) -> list[dict]:
    """Convert Chat Completions function schemas to Responses tool schemas."""
    converted = []
    for definition in definitions:
        function = definition["function"]
        converted.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object"}),
            "strict": False,
        })
    return converted


def messages_to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert persisted Chat Completions messages into Responses input items."""
    instructions: list[str] = []
    items: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if content:
                instructions.append(str(content))
            continue
        if role in {"user", "assistant"} and content:
            items.append({"role": role, "content": str(content)})
        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                function = tool_call["function"]
                items.append({
                    "type": "function_call",
                    "call_id": tool_call["id"],
                    "name": function["name"],
                    "arguments": function["arguments"],
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message["tool_call_id"],
                "output": str(content or ""),
            })
    return "\n\n".join(instructions), items


def serialized_checkpoint(method):
    """Use the same DB lock as cleanup for the complete state transition."""
    from functools import wraps
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.checkpoint_store.thread_guard(self.thread_id):
            return method(self, *args, **kwargs)
    return run


class LangGraphAgentRuntime:
    """Durable ReAct graph with HITL approval for sensitive write tools."""

    def __init__(
        self,
        *,
        client,
        model: str,
        temperature: float,
        tool_manager,
        max_steps: int,
        checkpoint_path: str,
        thread_key: str,
        hitl_enabled: bool = True,
        checkpoint_backend: str = "sqlite",
        database_url: str = "",
        tenant_id: str = "local",
        user_id: str = "default",
        event_sink: Callable[[dict], None] | None = None,
        tool_policy: ToolPolicy | None = None,
        approval_timeout_seconds: int = 86_400,
        checkpoint_store: CheckpointStore | None = None,
    ):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.tool_manager = tool_manager
        self.max_steps = max_steps
        self.hitl_enabled = hitl_enabled
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.event_sink = event_sink
        self.approval_timeout_seconds = approval_timeout_seconds
        self._tool_cache: dict[str, str] = {}
        self._cancel_check: Callable[[], bool] = lambda: False
        self.tool_executor = ReliableToolExecutor(
            tool_manager, tool_policy or ToolPolicy(), event_sink=self._emit
        )

        self._owns_checkpoint_store = checkpoint_store is None
        self.checkpoint_store = checkpoint_store or CheckpointStore(
            backend=checkpoint_backend,
            sqlite_path=checkpoint_path,
            database_url=database_url,
        )
        self._checkpointer = self.checkpoint_store.saver
        identity = f"{tenant_id}:{user_id}:{thread_key}"
        self.thread_id = sha256(identity.encode("utf-8")).hexdigest()
        self.checkpoint_store.assert_owner(self.thread_id, tenant_id, user_id)
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(AgentGraphState)
        builder.add_node("model", self._call_model)
        builder.add_node("tools", self._execute_tools)
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", self._after_model, {
            "tools": "tools",
            "end": END,
        })
        builder.add_edge("tools", "model")
        return builder.compile(checkpointer=self._checkpointer)

    @serialized_checkpoint
    def invoke(
        self,
        messages: list[dict],
        request_id: str = "",
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        self._cancel_check = cancel_check or (lambda: False)
        instructions, input_items = messages_to_responses_input(messages)
        state: AgentGraphState = {
            "instructions": instructions,
            "input_items": input_items,
            "response_items": [],
            "pending_calls": [],
            "final_text": "",
            "steps": 0,
            "events": [],
            "tool_history": [],
            "execution_context": {
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "request_id": request_id,
            },
        }
        self.checkpoint_store.touch(self.thread_id, self.tenant_id, self.user_id)
        self._emit({"type": "run_started", "request_id": request_id})
        result = self._invoke_graph(state)
        if result.get("__interrupt__"):
            self.checkpoint_store.mark_status(self.thread_id, "pending")
            payload = result["__interrupt__"][0].value
            self._emit({"type": "approval_required", "approval": payload})
        else:
            self.checkpoint_store.mark_status(self.thread_id, "completed")
        return result

    @serialized_checkpoint
    def resume(self, decisions: list[dict]) -> dict:
        self.checkpoint_store.touch(self.thread_id, self.tenant_id, self.user_id)
        result = self._invoke_graph(Command(resume={"decisions": decisions}))
        if result.get("__interrupt__"):
            self.checkpoint_store.mark_status(self.thread_id, "pending")
            payload = result["__interrupt__"][0].value
            self._emit({"type": "approval_required", "approval": payload})
        else:
            self.checkpoint_store.mark_status(self.thread_id, "completed")
        return result

    def _invoke_graph(self, value):
        try:
            return self.graph.invoke(value, config=self.config)
        except RunCancelled:
            raise
        except Exception:
            self.checkpoint_store.mark_status(self.thread_id, "failed")
            self._emit({"type": "run_failed", "error": "Agent 执行失败"})
            raise

    @serialized_checkpoint
    def cancel_pending(self, actor_id: str) -> None:
        snapshot = self.graph.get_state(self.config)
        if not any(task.interrupts for task in snapshot.tasks):
            raise RuntimeError("当前没有待审核操作")
        state = snapshot.values
        audit = getattr(self.tool_manager, "audit_tool_decision", None)
        for call in state.get("pending_calls", []):
            if call["name"] in SENSITIVE_TOOLS and audit:
                context = ToolExecutionContext(self.tenant_id, self.user_id,
                    state.get("execution_context", {}).get("request_id", ""),
                    f"{self.thread_id}:{call['call_id']}", actor_id)
                try:
                    audit(call["name"], json.loads(call["arguments"]), context, "reject")
                except RuntimeError:
                    # Already expired/rejected decisions remain non-executable.
                    pass
        self.graph.update_state(self.config, {"pending_calls": [], "final_text": "用户取消了操作"}, as_node="model")
        self.checkpoint_store.mark_status(self.thread_id, "completed")
        self._emit({"type": "run_cancelled"})

    def has_pending_interrupt(self) -> bool:
        snapshot = self.graph.get_state(self.config)
        return any(task.interrupts for task in snapshot.tasks)

    def _call_model(self, state: AgentGraphState) -> dict:
        self._raise_if_cancelled()
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": state.get("instructions") or None,
            "input": state.get("input_items", []),
            "temperature": self.temperature,
            "store": False,
            "parallel_tool_calls": False,
        }
        # The last model step must produce a final answer instead of starting
        # tool work that the graph has no remaining budget to execute.
        if state.get("steps", 0) < self.max_steps - 1:
            request["tools"] = responses_tool_definitions(
                self.tool_manager.tool_definitions
            )
        self._emit({"type": "model_started", "step": state.get("steps", 0) + 1})
        if self.event_sink is not None and hasattr(self.client.responses, "stream"):
            with self.client.responses.stream(**request) as stream:
                for event in stream:
                    self._raise_if_cancelled()
                    if event.type == "response.output_text.delta":
                        self._emit({"type": "token_delta", "delta": event.delta})
                response = stream.get_final_response()
        else:
            response = self.client.responses.create(**request)
            self._raise_if_cancelled()
        output_items = [item.model_dump(exclude_none=True) for item in response.output]
        calls = [
            {
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            }
            for item in response.output
            if item.type == "function_call"
        ]
        register = getattr(self.tool_manager, "register_tool_approval", None)
        if register:
            for call in calls:
                if call["name"] in SENSITIVE_TOOLS:
                    register(call["name"], json.loads(call["arguments"] or "{}"),
                             ToolExecutionContext(self.tenant_id, self.user_id,
                                                  state.get("execution_context", {}).get("request_id", ""),
                                                  f"{self.thread_id}:{call['call_id']}"),
                             self.approval_timeout_seconds)
        events = list(state.get("events", []))
        events.append({
            "type": "model_completed",
            "response_id": response.id,
            "tool_calls": [call["name"] for call in calls],
        })
        self._emit(events[-1])
        return {
            "response_items": output_items,
            "pending_calls": calls,
            "final_text": response.output_text or "",
            "steps": state.get("steps", 0) + 1,
            "events": events,
            "approval_requested_at": (
                time.time()
                if any(call["name"] in SENSITIVE_TOOLS for call in calls)
                else 0.0
            ),
        }

    def _after_model(self, state: AgentGraphState) -> str:
        if state.get("pending_calls") and state.get("steps", 0) < self.max_steps:
            return "tools"
        return "end"

    def _execute_tools(self, state: AgentGraphState) -> dict:
        calls = state.get("pending_calls", [])
        sensitive = [call for call in calls if call["name"] in SENSITIVE_TOOLS]
        decisions_by_call: dict[str, dict] = {}

        if sensitive and self.hitl_enabled:
            approval = interrupt({
                "type": "tool_approval",
                "actions": sensitive,
                "allowed_decisions": ["approve", "edit", "reject"],
            })
            supplied = approval.get("decisions", []) if isinstance(approval, dict) else []
            for call, decision in zip(sensitive, supplied):
                decisions_by_call[call["call_id"]] = decision
            requested_at = state.get("approval_requested_at", 0.0)
            if requested_at and time.time() - requested_at > self.approval_timeout_seconds:
                decisions_by_call = {
                    call["call_id"]: {
                        "type": "reject",
                        "message": "审批已超时，请重新发起退款请求",
                        "approved_by": "system-timeout",
                    }
                    for call in sensitive
                }

        outputs: list[dict] = []
        events = list(state.get("events", []))
        tool_history = list(state.get("tool_history", []))
        for call in calls:
            self._raise_if_cancelled()
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                arguments = {}
                decisions_by_call[call["call_id"]] = {
                    "type": "reject",
                    "message": f"工具参数不是合法 JSON: {exc}",
                }
            default_decision = (
                {"type": "reject", "message": "敏感操作缺少明确审批决定"}
                if call["name"] in SENSITIVE_TOOLS and self.hitl_enabled
                else {"type": "approve"}
            )
            decision = decisions_by_call.get(call["call_id"], default_decision)
            decision_type = decision.get("type", "reject")
            if decision_type == "edit":
                edited = decision.get("arguments", arguments)
                if not isinstance(edited, dict):
                    decision_type = "reject"
                    decision = {"type": "reject", "message": "编辑后的参数必须是对象"}
                else:
                    arguments = edited
            if call["name"] in SENSITIVE_TOOLS:
                execution = state.get("execution_context", {})
                audit_context = ToolExecutionContext(
                    tenant_id=execution.get("tenant_id", self.tenant_id),
                    user_id=execution.get("user_id", self.user_id),
                    request_id=execution.get("request_id", ""),
                    idempotency_key=f"{self.thread_id}:{call['call_id']}",
                    approved_by=str(decision.get("approved_by", "unknown")),
                )
                audit = getattr(self.tool_manager, "audit_tool_decision", None)
                if audit:
                    try:
                        audit(call["name"], arguments, audit_context, decision_type)
                    except RuntimeError as exc:
                        decision_type = "reject"
                        decision = {"type": "reject", "message": str(exc)}
            if decision_type == "reject":
                result = json.dumps({
                    "success": False,
                    "error": decision.get("message", "人工审核拒绝了该操作"),
                }, ensure_ascii=False)
            else:
                execution = state.get("execution_context", {})
                result = self._execute_one(
                    call["name"],
                    arguments,
                    ToolExecutionContext(
                        tenant_id=execution.get("tenant_id", self.tenant_id),
                        user_id=execution.get("user_id", self.user_id),
                        request_id=execution.get("request_id", ""),
                        idempotency_key=f"{self.thread_id}:{call['call_id']}",
                        approved_by=str(decision.get("approved_by", "system")),
                    ),
                )
            outputs.append({
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": result,
            })
            tool_history.append({
                "call_id": call["call_id"],
                "name": call["name"],
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "output": result,
                "decision": decision_type,
            })
            events.append({
                "type": "tool_completed",
                "name": call["name"],
                "call_id": call["call_id"],
                "decision": decision_type,
            })

        return {
            "input_items": (
                list(state.get("input_items", []))
                + list(state.get("response_items", []))
                + outputs
            ),
            "response_items": [],
            "pending_calls": [],
            "events": events,
            "tool_history": tool_history,
        }

    def _execute_one(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> str:
        cache_key = tool_cache_key(name, arguments)
        if cache_key is not None and cache_key in self._tool_cache:
            return self._tool_cache[cache_key]
        result = self.tool_executor.execute(name, arguments, context)
        if cache_key is None:
            self._tool_cache.clear()
        else:
            self._tool_cache[cache_key] = result
        return result

    def close(self) -> None:
        self.tool_executor.close()
        if self._owns_checkpoint_store:
            self.checkpoint_store.close()

    @serialized_checkpoint
    def reset(self) -> None:
        self.checkpoint_store.delete_thread(self.thread_id)

    def cleanup_checkpoints(self, retention_days: int) -> int:
        return self.checkpoint_store.cleanup(retention_days)

    def _emit(self, event: dict) -> None:
        if self.event_sink:
            self.event_sink(event)

    def _raise_if_cancelled(self) -> None:
        if self._cancel_check():
            self._emit({"type": "run_cancelled"})
            self.checkpoint_store.mark_status(self.thread_id, "failed")
            raise RunCancelled("Agent run 已取消")
