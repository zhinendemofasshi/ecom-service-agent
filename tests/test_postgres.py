"""Real PostgreSQL integration tests; each test owns an isolated schema.

Run with TEST_DATABASE_URL pointing at a development database. No public-schema
application data is modified or deleted by these tests.
"""
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.agent.checkpointing import CheckpointStore
from app.agent.tools.refund import apply_refund, register_refund_approval, record_refund_decision
from app.api import SessionRegistry, create_app
from app.config.settings import Settings
from app.database import DatabaseBusy, PostgresRunSlots, advisory_lock, pg_connect
from tests.test_api import headers
from tests.test_langgraph_runtime import FakeResponses, FakeToolManager, LangGraphAgentRuntime


@pytest.fixture
def postgres_url():
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL integration")
    schema = "ecom_test_" + uuid4().hex
    with pg_connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = make_conninfo(base, options=f"-csearch_path={schema}")
    try:
        yield url
    finally:
        with pg_connect(base) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def runtime(url, responses, manager=None):
    return LangGraphAgentRuntime(client=SimpleNamespace(responses=responses), model="fake", temperature=0,
        tool_manager=manager or FakeToolManager(), max_steps=5, checkpoint_path="unused.sqlite",
        thread_key="shared-thread", checkpoint_backend="postgres", database_url=url,
        tenant_id="tenant-a", user_id="user-xiaoming")


def test_checkpoint_restart_ownership_and_cleanup(postgres_url):
    responses = FakeResponses()
    original = runtime(postgres_url, responses)
    original.invoke([{"role": "user", "content": "refund"}])
    thread_id = original.thread_id
    original.close()
    restored = runtime(postgres_url, responses)
    try:
        assert restored.has_pending_interrupt()
        with pytest.raises(PermissionError):
            restored.checkpoint_store.touch(thread_id, "other", "user-xiaoming")
        assert restored.checkpoint_store.cleanup(0) == 0  # pending is retained
        restored.resume([{"type": "approve"}])
        with pg_connect(postgres_url) as connection:
            connection.execute("UPDATE agent_threads SET updated_at = now() - interval '40 days'")
        with advisory_lock(postgres_url, "checkpoint-thread", thread_id):
            assert restored.checkpoint_store.cleanup(30) == 0
        assert restored.checkpoint_store.cleanup(30) == 1
        assert restored.checkpoint_store.saver.get_tuple(restored.config) is None
        with pg_connect(postgres_url) as connection:
            for table in ("agent_threads", "checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        restored.close()


def test_concurrent_initialization_and_event_cursors(postgres_url):
    def create(_):
        return SessionRegistry("unused", postgres_url)
    with ThreadPoolExecutor(4) as pool:
        registries = list(pool.map(create, range(4)))
    try:
        sid = registries[0].create("tenant-a", "user-xiaoming", "/shared/session.json")
        def publish(i):
            return registries[i % 4].append_event(sid, {"timestamp": "2026-09-13T00:00:00+00:00", "type": "token_delta"}, 2000)
        with ThreadPoolExecutor(8) as pool:
            events = list(pool.map(publish, range(40)))
        assert sorted(e["sequence"] for e in events) == list(range(1, 41))
        assert len(registries[1].events_after(sid, 20)) == 20
        registries[0].create_run("shared-run", sid, "tenant-a", "user-xiaoming")
        assert registries[1].cancel_run("shared-run", "tenant-a", "user-xiaoming") == "cancelling"
        assert registries[0].is_cancel_requested("shared-run")
    finally:
        for registry in registries:
            registry.close()


def grant(url, key, order="ORD-20240115-001", user="user-xiaoming"):
    args = dict(db_path=url, idempotency_key=key, tenant_id="tenant-a", user_id=user, order_id=order, reason="质量问题")
    register_refund_approval(**args, timeout_seconds=60)
    record_refund_decision(**args, decision="approve", actor_id="reviewer", approver_ids=["reviewer"])
    return args


def test_refund_transactions_parallel_idempotency_and_rollback(postgres_url):
    args = grant(postgres_url, "parallel")
    def refund(_):
        return apply_refund(**args, approved_by="reviewer", approver_ids=["reviewer"])
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(refund, range(6)))
    assert all(r["success"] for r in results)
    assert sum(not r.get("idempotent_replay") for r in results) == 1
    with pg_connect(postgres_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM refund_approvals").fetchone()[0] == "consumed"
    with pytest.raises(RuntimeError, match="重复"):
        record_refund_decision(**args, decision="approve", actor_id="reviewer", approver_ids=["reviewer"])
    # Force audit failure: refund insert and approval consumption must roll back too.
    other = grant(postgres_url, "rollback", "ORD-20240110-003", "user-dazhuang")
    with pg_connect(postgres_url) as connection:
        connection.execute("ALTER TABLE refund_audit ADD CONSTRAINT reject_success CHECK (outcome != 'accepted') NOT VALID")
    with pytest.raises(psycopg.errors.CheckViolation):
        apply_refund(**other, approved_by="reviewer", approver_ids=["reviewer"])
    with pg_connect(postgres_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refund_requests WHERE idempotency_key='rollback'").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM refund_approvals WHERE idempotency_key='rollback'").fetchone()[0] == "approve"


def test_shared_run_capacity_and_session_locks(postgres_url):
    first, second = PostgresRunSlots(postgres_url, 1), PostgresRunSlots(postgres_url, 1)
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()
    with advisory_lock(postgres_url, "api-user", "tenant:user"):
        with pytest.raises(DatabaseBusy):
            with advisory_lock(postgres_url, "api-user", "tenant:user"):
                pass
    with advisory_lock(postgres_url, "api-user", "tenant:user"):
        pass


class StatefulAgent:
    """Use the real agent with offline model/structured extraction only."""
    def __new__(cls, **kwargs):
        from app.agent.chat import EcomAgent
        from app.schemas.response import CustomerServiceResponse, IntentType
        from tests.test_langgraph_runtime import FakeOutputItem
        class Responses:
            def create(self, **request):
                inputs = request["input"]
                if not any(item.get("type") == "function_call_output" for item in inputs):
                    output = FakeOutputItem(item_type="function_call", call_id="real-refund",
                        name="apply_refund", arguments=json.dumps({"order_id":"ORD-20240115-001", "reason":"质量问题"}))
                    return SimpleNamespace(id="fake", output=[output], output_text="")
                return SimpleNamespace(id="done", output=[FakeOutputItem(item_type="message", text="done")], output_text="done")
        class Agent(EcomAgent):
            def _extract_structured_response(self, text):
                return CustomerServiceResponse(intent=IntentType.RETURN_REQUEST, confidence=1, reply=text, requires_human=False)
        kwargs["client"] = SimpleNamespace(responses=Responses())
        return Agent(**kwargs)


def test_two_api_instances_restore_refund_and_conversation(postgres_url):
    from fastapi.testclient import TestClient
    config = Settings(_env_file=None, checkpoint_backend="postgres", database_url=postgres_url,
        trusted_identity_headers=True, service_api_key="test-secret", approver_api_key="approval-secret",
        refund_approver_ids=["actor-1"], memory_enabled=False, skills_enabled=False)
    with TestClient(create_app(config, StatefulAgent)) as a, TestClient(create_app(config, StatefulAgent)) as b:
        sid = a.post("/sessions", headers=headers()).json()["session_id"]
        response = a.post(f"/sessions/{sid}/messages", headers={**headers(), "X-Request-ID":"refund"}, json={"text":"退款"})
        assert response.status_code == 200, response.text
        assert response.json()["pending_approval"]
        assert b.get("/runs/refund", headers=headers()).json()["status"] == "pending"
        approved = b.post("/runs/refund/approve", headers=headers(approval=True))
        assert approved.status_code == 200, approved.text
        assert not approved.json()["pending_approval"]
        assert a.post("/runs/refund/approve", headers=headers(approval=True)).status_code == 409
        assert a.get("/runs/refund", headers=headers()).json()["status"] == "completed"
        # Worker A's cached history is stale; the next operation reloads B's document.
        from app.api import Identity
        manager = a.app.state.session_manager
        identity = Identity("tenant-a", "user-xiaoming", "actor-1", True, False)
        managed = manager.get(sid, identity)
        with manager.execution(sid, identity, managed):
            assert any(m.get("role") == "tool" for m in managed.agent.raw_messages)
        events = b.get(f"/sessions/{sid}/events?limit=100&timeout_seconds=.1", headers=headers())
        assert "event: approval_required" in events.text and "event: run_completed" in events.text
    with pg_connect(postgres_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_documents WHERE namespace='session'").fetchone()[0] == 0


def test_sqlite_migration_preserves_pending_checkpoint_and_is_repeatable(postgres_url, tmp_path):
    from app.scripts.migrate_postgres import migrate
    from app.agent.storage import save_session, load_session
    checkpoint = str(tmp_path / "checkpoint.sqlite")
    api_path = str(tmp_path / "api.sqlite")
    refund_path = str(tmp_path / "refund.sqlite")
    cli_path = str(tmp_path / "session.json")
    source = LangGraphAgentRuntime(client=SimpleNamespace(responses=FakeResponses()), model="fake", temperature=0,
        tool_manager=FakeToolManager(), max_steps=5, checkpoint_path=checkpoint,
        thread_key="shared-thread", tenant_id="tenant-a", user_id="user-xiaoming")
    source.invoke([{"role":"user", "content":"refund"}])
    source.close()
    registry = SessionRegistry(api_path)
    sid = registry.create("tenant-a", "user-xiaoming", cli_path + ".api")
    registry.append_event(sid, {"type":"approval_required", "timestamp":"2026-09-13T00:00:00+00:00"}, 2000)
    registry.close()
    grant(refund_path, "existing-approval")
    save_session(cli_path, [{"role":"user", "content":"saved"}], "summary")
    config = Settings(_env_file=None, database_url=postgres_url,
        api_session_db_path=api_path, refund_db_path=refund_path, langgraph_checkpoint_path=checkpoint,
        session_path=cli_path, memory_dir=str(tmp_path / "memory"))
    plan = migrate(config)
    assert plan["counts"]["checkpoints"] > 0
    applied = migrate(config, apply=True, backup_dir=str(tmp_path / "backup"))
    assert applied["mode"] == "applied"
    repeat = migrate(config, apply=True, backup_dir=str(tmp_path / "repeat-backup"))
    assert repeat["mode"] == "already_applied"
    restored = runtime(postgres_url, SimpleNamespace(create=lambda **_: SimpleNamespace(id="done", output=[], output_text="done")))
    try:
        assert restored.has_pending_interrupt()
        assert restored.resume([{"type":"approve"}])["final_text"] == "done"
    finally:
        restored.close()
    doc = load_session(cli_path, database_url=postgres_url, storage_key=json.dumps(["local", "default", cli_path]))
    assert doc["summary"] == "summary"
    save_session(cli_path, [{"role":"user", "content":"new source data"}], "changed")
    with pytest.raises(RuntimeError, match="Source changed"):
        migrate(config, apply=True, backup_dir=str(tmp_path / "changed-backup"))
    with pg_connect(postgres_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM storage_migrations").fetchone()[0] == 1


def test_parallel_user_daily_limit_and_expiry(postgres_url, monkeypatch):
    from app.agent.tools.mock_data import ORDERS
    from copy import deepcopy
    order = deepcopy(ORDERS["ORD-20240115-001"])
    monkeypatch.setitem(ORDERS, "extra-order", order)
    first = grant(postgres_url, "daily-one")
    second = grant(postgres_url, "daily-two", "extra-order")
    def run(args):
        return apply_refund(**args, approved_by="reviewer", approver_ids=["reviewer"], max_refunds_per_user_per_day=1)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [first, second]))
    assert sum(r["success"] for r in results) == 1
    assert any(r.get("error_code") == "daily_limit_exceeded" for r in results)
    expired = grant(postgres_url, "expired", "ORD-20240110-003", "user-dazhuang")
    with pg_connect(postgres_url) as connection:
        connection.execute("UPDATE refund_approvals SET expires_at=0 WHERE idempotency_key='expired'")
    assert apply_refund(**expired, approved_by="reviewer", approver_ids=["reviewer"])["error_code"] == "approval_invalid"


def test_abandoned_runs_reconciled_without_replaying_or_interrupting_live_owner(postgres_url):
    registry = SessionRegistry("unused", postgres_url)
    sid = registry.create("tenant-a", "user-xiaoming", "/shared/session")
    registry.create_run("abandoned", sid, "tenant-a", "user-xiaoming")
    with pg_connect(postgres_url) as connection:
        connection.execute("UPDATE api_runs SET updated_at='2000-01-01T00:00:00+00:00'")
    try:
        with advisory_lock(postgres_url, "api-user", "tenant-a:user-xiaoming"):
            assert registry.recover_abandoned(300) == 0
        assert registry.recover_abandoned(300) == 1
        assert registry.get_run("abandoned", "tenant-a", "user-xiaoming")["status"] == "failed"
        assert registry.events_after(sid, 0)[0]["type"] == "run_failed"
        assert registry.recover_abandoned(300) == 0
    finally:
        registry.close()


def _approve_in_separate_process(url, run_id):
    from fastapi.testclient import TestClient
    config = Settings(_env_file=None, checkpoint_backend="postgres", database_url=url,
        trusted_identity_headers=True, service_api_key="test-secret", approver_api_key="approval-secret",
        refund_approver_ids=["actor-1"], memory_enabled=False, skills_enabled=False)
    with TestClient(create_app(config, StatefulAgent)) as client:
        response = client.post(f"/runs/{run_id}/approve", headers=headers(approval=True))
        return response.status_code, response.json()


def test_approval_handoff_to_separate_worker_process(postgres_url):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from fastapi.testclient import TestClient
    config = Settings(_env_file=None, checkpoint_backend="postgres", database_url=postgres_url,
        trusted_identity_headers=True, service_api_key="test-secret", approver_api_key="approval-secret",
        refund_approver_ids=["actor-1"], memory_enabled=False, skills_enabled=False)
    with TestClient(create_app(config, StatefulAgent)) as first:
        sid = first.post("/sessions", headers=headers()).json()["session_id"]
        first.post(f"/sessions/{sid}/messages", headers={**headers(), "X-Request-ID":"process-run"}, json={"text":"退款"})
        with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
            code, body = pool.submit(_approve_in_separate_process, postgres_url, "process-run").result(timeout=20)
        assert code == 200, body
        assert not body["pending_approval"]
        assert first.get("/runs/process-run", headers=headers()).json()["status"] == "completed"
    with pg_connect(postgres_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == 1


def test_p1_web_approval_expiry_and_notification(postgres_url):
    from fastapi.testclient import TestClient
    config = Settings(_env_file=None, checkpoint_backend="postgres", database_url=postgres_url,
        multi_agent_enabled=True, trusted_identity_headers=True, service_api_key="test-secret",
        approver_api_key="approval-secret", refund_approver_ids=["actor-1"],
        memory_enabled=False, skills_enabled=False)
    with TestClient(create_app(config, StatefulAgent)) as client:
        assert client.get("/approvals").status_code == 200
        sid = client.post("/sessions", headers=headers()).json()["session_id"]
        assert client.get(f"/sessions/{sid}/approvals").status_code == 401
        response = client.post(f"/sessions/{sid}/messages",
            headers={**headers(), "X-Request-ID": "expiring"}, json={"text": "退款"})
        assert response.status_code == 200, response.text
        actions = client.get(f"/sessions/{sid}/approvals", headers=headers()).json()["actions"]
        assert actions[0]["order_id"] == "ORD-20240115-001"
        assert actions[0]["amount"] > 0
        with pg_connect(postgres_url) as connection:
            connection.execute("UPDATE refund_approvals SET expires_at=0")
        client.app.state.session_manager.expire_pending()
        assert client.get("/runs/expiring", headers=headers()).json()["status"] == "expired"
        assert client.get(f"/sessions/{sid}/approvals", headers=headers()).json()["actions"] == []
        events = client.get(f"/sessions/{sid}/events?timeout_seconds=.1", headers=headers())
        assert "approval_expired" in events.text
        assert client.post("/runs/expiring/approve", headers=headers(approval=True)).status_code == 409
    with pg_connect(postgres_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == 0


def test_p1_complaint_handoff_queue(postgres_url):
    from fastapi.testclient import TestClient
    config = Settings(_env_file=None, checkpoint_backend="postgres", database_url=postgres_url,
        multi_agent_enabled=True, trusted_identity_headers=True, service_api_key="test-secret",
        approver_api_key="approval-secret", memory_enabled=False, skills_enabled=False)
    with TestClient(create_app(config, StatefulAgent)) as client:
        sid = client.post("/sessions", headers=headers()).json()["session_id"]
        response = client.post(f"/sessions/{sid}/messages", headers=headers(),
            json={"text": "我要投诉，商品导致受伤"})
        assert response.status_code == 200, response.text
        assert response.json()["response"]["requires_human"]
        items = client.get(f"/sessions/{sid}/handoffs", headers=headers()).json()["items"]
        assert len(items) == 1 and items[0]["status"] == "open"
