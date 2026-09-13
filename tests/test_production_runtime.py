import json
import sqlite3
import time
from types import SimpleNamespace

from app.agent.checkpointing import CheckpointStore
from app.agent.tools.executor import ReliableToolExecutor, ToolExecutionContext, ToolPolicy
from app.agent.tools.refund import apply_refund, register_refund_approval
import pytest
from app.config.settings import settings


@pytest.fixture(autouse=True)
def reviewers(monkeypatch):
    monkeypatch.setattr(settings, "refund_approver_ids", ["reviewer", "reviewer-2"])


def grant(db_path, key, order="ORD-20240115-001", user="user-xiaoming", reason="质量问题"):
    register_refund_approval(db_path=db_path, idempotency_key=key, tenant_id="tenant-a",
                             user_id=user, order_id=order, reason=reason, timeout_seconds=60)
    record_refund_decision(db_path=db_path, idempotency_key=key, tenant_id="tenant-a",
                           user_id=user, order_id=order, reason=reason, decision="approve", actor_id="reviewer")

from app.agent.tools.refund import record_refund_decision
from app.agent.tools.manager import ToolManager


def context(key="idem-1"):
    return ToolExecutionContext("tenant-a", "user-xiaoming", "req-1", key, "reviewer-1")


def test_refund_requires_owner_and_is_idempotent(tmp_path):
    db_path = str(tmp_path / "refunds.sqlite")
    forbidden = apply_refund(
        "ORD-20240115-001", "质量问题",
        tenant_id="tenant-a", user_id="其他人", idempotency_key="bad",
        approved_by="reviewer", db_path=db_path,
    )
    assert forbidden["error_code"] == "forbidden"

    kwargs = dict(
        order_id="ORD-20240115-001", reason="质量问题",
        tenant_id="tenant-a", user_id="user-xiaoming", idempotency_key="idem-1",
        approved_by="reviewer", db_path=db_path,
    )
    grant(db_path, "idem-1")
    first = apply_refund(**kwargs)
    second = apply_refund(**kwargs)
    assert first["success"] is True
    assert second["idempotent_replay"] is True
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM refund_audit").fetchone()[0] == 5

    register_refund_approval(db_path=db_path, idempotency_key="idem-rejected", tenant_id="tenant-a",
                             user_id="user-xiaoming", order_id="ORD-20240115-001", reason="changed mind", timeout_seconds=60)
    record_refund_decision(
        db_path=db_path,
        idempotency_key="idem-rejected",
        tenant_id="tenant-a",
        user_id="user-xiaoming",
        order_id="ORD-20240115-001",
        reason="changed mind",
        decision="reject",
        actor_id="reviewer-2",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT outcome FROM refund_audit WHERE idempotency_key = 'idem-rejected' AND action = 'approval'"
        ).fetchone()[0] == "reject"


def test_refund_enforces_amount_and_daily_limits(tmp_path):
    db_path = str(tmp_path / "limits.sqlite")
    grant(db_path, "amount", "ORD-20240110-003", "user-dazhuang")
    grant(db_path, "daily")
    amount_limited = apply_refund(
        "ORD-20240110-003", "质量问题",
        tenant_id="tenant-a", user_id="user-dazhuang", idempotency_key="amount",
        approved_by="reviewer", db_path=db_path, max_refund_amount=1000,
    )
    assert amount_limited["error_code"] == "amount_limit_exceeded"
    daily_limited = apply_refund(
        "ORD-20240115-001", "质量问题",
        tenant_id="tenant-a", user_id="user-xiaoming", idempotency_key="daily",
        approved_by="reviewer", db_path=db_path, max_refunds_per_user_per_day=0,
    )
    assert daily_limited["error_code"] == "daily_limit_exceeded"


class FlakyManager:
    def __init__(self):
        self.calls = 0

    def execute_tool(self, name, arguments, execution_context=None):
        self.calls += 1
        if self.calls < 2:
            raise ConnectionError("temporary")
        return json.dumps({"success": True})


def test_read_tool_retries_but_write_tool_does_not():
    policy = ToolPolicy(timeout_seconds=1, max_retries=2, failure_threshold=3)
    read_manager = FlakyManager()
    executor = ReliableToolExecutor(read_manager, policy)
    try:
        assert json.loads(executor.execute("query_order", {}, context()))["success"]
        assert read_manager.calls == 2
    finally:
        executor.close()

    write_manager = FlakyManager()
    executor = ReliableToolExecutor(write_manager, policy)
    try:
        result = json.loads(executor.execute("apply_refund", {}, context()))
        assert result["error_code"] == "tool_failed"
        assert write_manager.calls == 1
    finally:
        executor.close()


def test_tool_timeout_and_circuit_breaker():
    class SlowManager:
        def execute_tool(self, name, arguments, execution_context=None):
            time.sleep(0.05)
            return "{}"

    executor = ReliableToolExecutor(
        SlowManager(),
        ToolPolicy(timeout_seconds=0.005, max_retries=0, failure_threshold=1, recovery_seconds=60),
    )
    try:
        first = json.loads(executor.execute("query_order", {}, context()))
        second = json.loads(executor.execute("query_order", {}, context()))
        assert first["error_code"] == "tool_failed"
        assert second["error_code"] == "circuit_open"
    finally:
        executor.close()


def test_transient_error_payload_is_retried():
    class PayloadManager:
        def __init__(self):
            self.calls = 0

        def execute_tool(self, name, arguments, execution_context=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"success": False, "error_code": "connection_error", "error": "down"})
            return json.dumps({"success": True})

    manager = PayloadManager()
    executor = ReliableToolExecutor(
        manager, ToolPolicy(timeout_seconds=1, max_retries=1, failure_threshold=3)
    )
    try:
        assert json.loads(executor.execute("query_order", {}, context()))["success"]
        assert manager.calls == 2
    finally:
        executor.close()


def test_tool_input_and_output_limits():
    class Manager:
        def execute_tool(self, name, arguments, execution_context=None):
            return "x" * 20

    executor = ReliableToolExecutor(
        Manager(),
        ToolPolicy(max_input_bytes=10, max_output_bytes=10),
    )
    try:
        oversized_input = json.loads(
            executor.execute("query_order", {"value": "x" * 20}, context())
        )
        assert oversized_input["error_code"] == "tool_input_too_large"
        oversized_output = json.loads(executor.execute("query_order", {}, context()))
        assert oversized_output["error_code"] == "tool_output_too_large"
    finally:
        executor.close()


def test_checkpoint_ownership_and_cleanup(tmp_path):
    path = str(tmp_path / "checkpoints.sqlite")
    store = CheckpointStore(backend="sqlite", sqlite_path=path, database_url="")
    try:
        store.touch("thread-1", "tenant-a", "user-a")
        store.assert_owner("thread-1", "tenant-a", "user-a")
        try:
            store.assert_owner("thread-1", "tenant-b", "user-a")
            assert False, "expected ownership failure"
        except PermissionError:
            pass
        store.connection.execute(
            "UPDATE agent_threads SET updated_at = '2000-01-01T00:00:00+00:00'"
        )
        store.connection.commit()
        assert store.cleanup(30) == 0
        store.mark_status("thread-1", "completed")
        assert store.cleanup(30) == 1
    finally:
        store.close()


def test_postgres_backend_requires_url(tmp_path):
    try:
        CheckpointStore(backend="postgres", sqlite_path=str(tmp_path / "x"), database_url="")
        assert False, "expected configuration failure"
    except ValueError:
        pass


def test_memory_tool_is_scoped_to_each_tool_manager():
    first = ToolManager()
    second = ToolManager()
    memory_one = SimpleNamespace(
        memory_enabled=True,
        stm=SimpleNamespace(facts=["user one"]),
        ltm=SimpleNamespace(facts=[], interaction_summaries=[]),
    )
    memory_two = SimpleNamespace(
        memory_enabled=True,
        stm=SimpleNamespace(facts=["user two"]),
        ltm=SimpleNamespace(facts=[], interaction_summaries=[]),
    )
    first.bind_agent_context(memory_manager=memory_one)
    second.bind_agent_context(memory_manager=memory_two)
    assert json.loads(first.execute_tool("recall_user_memory", {}))["short_term_facts"] == ["user one"]
    assert json.loads(second.execute_tool("recall_user_memory", {}))["short_term_facts"] == ["user two"]


def test_refund_cannot_bypass_service_approval(tmp_path):
    db = str(tmp_path / "guard.sqlite")
    args = dict(tenant_id="tenant-a", user_id="user-xiaoming", idempotency_key="guard",
                approved_by="reviewer", db_path=db)
    assert apply_refund("ORD-20240115-001", "质量问题", **args)["error_code"] == "approval_invalid"
    register_refund_approval(db_path=db, idempotency_key="guard", tenant_id="tenant-a",
                             user_id="user-xiaoming", order_id="ORD-20240115-001", reason="质量问题", timeout_seconds=60)
    with pytest.raises(RuntimeError, match="权限"):
        record_refund_decision(db_path=db, idempotency_key="guard", tenant_id="tenant-a", user_id="user-xiaoming",
                               order_id="ORD-20240115-001", reason="质量问题", decision="approve", actor_id="imposter")
    record_refund_decision(db_path=db, idempotency_key="guard", tenant_id="tenant-a", user_id="user-xiaoming",
                           order_id="ORD-20240115-001", reason="质量问题", decision="reject", actor_id="user-xiaoming")
    assert apply_refund("ORD-20240115-001", "质量问题", **args)["error_code"] == "approval_invalid"
    with pytest.raises(RuntimeError, match="重复"):
        record_refund_decision(db_path=db, idempotency_key="guard", tenant_id="tenant-a", user_id="user-xiaoming",
                               order_id="ORD-20240115-001", reason="质量问题", decision="approve", actor_id="reviewer")


def test_refund_expiry_checked_at_execution_and_concurrent_replay(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    db = str(tmp_path / "concurrent.sqlite")
    grant(db, "parallel")
    args = dict(tenant_id="tenant-a", user_id="user-xiaoming", idempotency_key="parallel",
                approved_by="reviewer", db_path=db)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE refund_approvals SET expires_at = 0")
    assert apply_refund("ORD-20240115-001", "质量问题", **args)["error_code"] == "approval_invalid"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE refund_approvals SET expires_at = ?", (time.time() + 60,))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: apply_refund("ORD-20240115-001", "质量问题", **args), range(4)))
    assert all(r["success"] for r in results)
    assert sum(not r.get("idempotent_replay") for r in results) == 1


def test_permanent_tool_errors_are_never_retried():
    class InvalidManager:
        calls = 0
        def execute_tool(self, *args):
            self.calls += 1
            raise ValueError("bad input")
    manager = InvalidManager()
    result = json.loads(ReliableToolExecutor(manager, ToolPolicy()).execute("query_order", {}, context()))
    assert result["error_code"] == "non_retryable_error"
    assert manager.calls == 1


def test_tool_capacity_is_held_after_timeout(monkeypatch):
    import threading
    from app.agent.tools import executor as module
    monkeypatch.setattr(module, "_SHARED_TOOL_SLOTS", threading.BoundedSemaphore(1))
    released = threading.Event()
    class Blocking:
        def execute_tool(self, *args):
            released.wait(1)
            return "{}"
    executor = ReliableToolExecutor(Blocking(), ToolPolicy(timeout_seconds=.01, max_retries=0))
    try:
        executor.execute("apply_refund", {}, context())
        result = json.loads(executor.execute("apply_refund", {}, context()))
        assert result["error_code"] == "tool_concurrency_limit"
    finally:
        released.set()
        # Wait for the worker's capacity callback before monkeypatch restores the semaphore.
        assert module._SHARED_TOOL_SLOTS.acquire(timeout=1)
        module._SHARED_TOOL_SLOTS.release()
