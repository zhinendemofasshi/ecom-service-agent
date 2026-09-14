"""Reliable execution boundary for local and MCP tools."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from typing import Callable

from app.agent.tools.execution import READ_ONLY_TOOLS
from app.observability import span, identifier


_SHARED_TOOL_SLOTS = threading.BoundedSemaphore(32)
_SHARED_TOOL_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="agent-tool")

@dataclass(frozen=True)
class ToolExecutionContext:
    tenant_id: str
    user_id: str
    request_id: str
    idempotency_key: str
    approved_by: str = ""


@dataclass(frozen=True)
class ToolPolicy:
    timeout_seconds: float = 10.0
    max_retries: int = 2
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    max_input_bytes: int = 64_000
    max_output_bytes: int = 1_000_000


class ReliableToolExecutor:
    def __init__(
        self,
        tool_manager,
        policy: ToolPolicy,
        event_sink: Callable[[dict], None] | None = None,
    ):
        self.tool_manager = tool_manager
        self.policy = policy
        self.event_sink = event_sink
        self._pool = _SHARED_TOOL_POOL
        self._lock = threading.Lock()
        self._circuits: dict[str, tuple[int, float]] = {}

    def execute(self, name: str, arguments: dict, context: ToolExecutionContext) -> str:
        with span("tool.execute", "tool", metadata={
            "tool_name": name if name in {d["function"]["name"] for d in getattr(self.tool_manager, "tool_definitions", [])} else "unregistered",
            "request_id": identifier(context.request_id),
            "call_id": identifier(context.idempotency_key),
        }) as trace:
            result = self._execute(name, arguments, context)
            try:
                payload = json.loads(result)
                success = isinstance(payload, dict) and payload.get("success") is True
            except (ValueError, TypeError):
                success = False
            trace.outputs["success"] = success
            if not success:
                trace.error = "ToolBusinessError"
            return result

    def _execute(self, name: str, arguments: dict, context: ToolExecutionContext) -> str:
        input_size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
        if input_size > self.policy.max_input_bytes:
            return self._error("tool_input_too_large", "工具输入超过大小限制")
        if self._circuit_open(name):
            return self._error("circuit_open", f"工具 {name} 暂时不可用")
        attempts = 1 + (self.policy.max_retries if name in READ_ONLY_TOOLS else 0)
        last_error = ""
        for attempt in range(1, attempts + 1):
            self._emit({"type": "tool_started", "name": name, "attempt": attempt})
            if not _SHARED_TOOL_SLOTS.acquire(blocking=False):
                return self._error("tool_concurrency_limit", "工具并发达到上限")
            try:
                future = self._pool.submit(self.tool_manager.execute_tool, name, arguments, context)
            except Exception:
                _SHARED_TOOL_SLOTS.release()
                raise
            # A timed out write may still be running: hold capacity until it actually exits.
            future.add_done_callback(lambda _: _SHARED_TOOL_SLOTS.release())
            retryable = False
            try:
                with span("tool.attempt", "tool", metadata={"attempt": attempt}):
                    result = future.result(timeout=self.policy.timeout_seconds)
                if len(result.encode("utf-8")) > self.policy.max_output_bytes:
                    return self._error("tool_output_too_large", "工具输出超过大小限制")
                if self._is_transient_failure(result):
                    raise ConnectionError(json.loads(result).get("error", "transient tool error"))
                self._record_success(name)
                self._emit({"type": "tool_completed", "name": name, "attempt": attempt})
                return result
            except TimeoutError:
                future.cancel()
                last_error = f"工具执行超过 {self.policy.timeout_seconds:g} 秒"
                retryable = True
            except Exception as exc:  # noqa: BLE001 - normalized at the boundary
                last_error = f"{type(exc).__name__}: {exc}"
                retryable = isinstance(exc, (ConnectionError, TimeoutError))
            self._record_failure(name)
            self._emit({
                "type": "tool_failed",
                "name": name,
                "attempt": attempt,
                "error": last_error,
            })
            if not retryable:
                return self._error("non_retryable_error", last_error)
            if attempt < attempts:
                time.sleep(min(0.1 * (2 ** (attempt - 1)), 1.0))
        return self._error("tool_failed", last_error)

    def _circuit_open(self, name: str) -> bool:
        with self._lock:
            failures, opened_at = self._circuits.get(name, (0, 0.0))
            if failures < self.policy.failure_threshold:
                return False
            if time.monotonic() - opened_at >= self.policy.recovery_seconds:
                self._circuits[name] = (0, 0.0)
                return False
            return True

    def _record_success(self, name: str) -> None:
        with self._lock:
            self._circuits.pop(name, None)

    def _record_failure(self, name: str) -> None:
        with self._lock:
            failures, _ = self._circuits.get(name, (0, 0.0))
            failures += 1
            self._circuits[name] = (
                failures,
                time.monotonic() if failures >= self.policy.failure_threshold else 0.0,
            )

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({"success": False, "error_code": code, "error": message}, ensure_ascii=False)

    @staticmethod
    def _is_transient_failure(result: str) -> bool:
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("error_code") in {
            "connection_error",
            "upstream_timeout",
            "rate_limited",
        }

    def _emit(self, event: dict) -> None:
        if self.event_sink:
            self.event_sink(event)

    def close(self) -> None:
        # The bounded pool is process-wide to prevent one pool per user session.
        pass
