"""LangGraph execution runtime backed by the OpenAI Responses API.

The graph owns orchestration and durable interruption. Business tools remain in
ToolManager, so authorization, idempotency, and transactions stay outside the
model-facing runtime.
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.tools.execution import tool_cache_key


SENSITIVE_TOOLS = {"apply_refund"}


class AgentGraphState(TypedDict, total=False):
    instructions: str
    input_items: list[dict[str, Any]]
    response_items: list[dict[str, Any]]
    pending_calls: list[dict[str, Any]]
    final_text: str
    steps: int
    events: list[dict[str, Any]]
    tool_history: list[dict[str, Any]]


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
    ):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.tool_manager = tool_manager
        self.max_steps = max_steps
        self.hitl_enabled = hitl_enabled
        self._tool_cache: dict[str, str] = {}

        path = Path(checkpoint_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._checkpointer = SqliteSaver(self._connection)
        self.thread_id = sha256(thread_key.encode("utf-8")).hexdigest()
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

    def invoke(self, messages: list[dict]) -> dict:
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
        }
        return self.graph.invoke(state, config=self.config)

    def resume(self, decisions: list[dict]) -> dict:
        return self.graph.invoke(
            Command(resume={"decisions": decisions}),
            config=self.config,
        )

    def has_pending_interrupt(self) -> bool:
        snapshot = self.graph.get_state(self.config)
        return any(task.interrupts for task in snapshot.tasks)

    def _call_model(self, state: AgentGraphState) -> dict:
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": state.get("instructions") or None,
            "input": state.get("input_items", []),
            "temperature": self.temperature,
            "store": False,
        }
        # The last model step must produce a final answer instead of starting
        # tool work that the graph has no remaining budget to execute.
        if state.get("steps", 0) < self.max_steps - 1:
            request["tools"] = responses_tool_definitions(
                self.tool_manager.tool_definitions
            )
        response = self.client.responses.create(
            **request,
        )
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
        events = list(state.get("events", []))
        events.append({
            "type": "model_completed",
            "response_id": response.id,
            "tool_calls": [call["name"] for call in calls],
        })
        return {
            "response_items": output_items,
            "pending_calls": calls,
            "final_text": response.output_text or "",
            "steps": state.get("steps", 0) + 1,
            "events": events,
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

        outputs: list[dict] = []
        events = list(state.get("events", []))
        tool_history = list(state.get("tool_history", []))
        for call in calls:
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
            if decision_type == "reject":
                result = json.dumps({
                    "success": False,
                    "error": decision.get("message", "人工审核拒绝了该操作"),
                }, ensure_ascii=False)
            else:
                result = self._execute_one(call["name"], arguments)
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

    def _execute_one(self, name: str, arguments: dict) -> str:
        cache_key = tool_cache_key(name, arguments)
        if cache_key is not None and cache_key in self._tool_cache:
            return self._tool_cache[cache_key]
        result = self.tool_manager.execute_tool(name, arguments)
        if cache_key is None:
            self._tool_cache.clear()
        else:
            self._tool_cache[cache_key] = result
        return result

    def close(self) -> None:
        self._connection.close()

    def reset(self) -> None:
        self._checkpointer.delete_thread(self.thread_id)
