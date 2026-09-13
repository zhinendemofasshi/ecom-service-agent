from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import create_app
from app.config.settings import Settings
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    def __init__(self, **kwargs):
        self.event_sink = kwargs["event_sink"]
        self._pending = False
        self.graph_runtime = None
        self.tool_manager = SimpleNamespace(close=lambda: None)

    @property
    def has_pending_approval(self):
        return self._pending

    def chat(self, text, request_id="", cancel_check=None):
        self.event_sink({"type": "model_started", "request_id": request_id})
        self.event_sink({"type": "token_delta", "delta": "你好"})
        self._pending = "退款" in text
        return CustomerServiceResponse(
            intent=IntentType.RETURN_REQUEST if self._pending else IntentType.GREETING,
            confidence=1,
            reply="等待审批" if self._pending else "你好",
            requires_human=self._pending,
        )

    def resume_approval(self, approved, **kwargs):
        if not self._pending:
            raise RuntimeError("当前没有待审核操作")
        self._pending = False
        return CustomerServiceResponse(
            intent=IntentType.RETURN_REQUEST,
            confidence=1,
            reply="已批准" if approved else "已拒绝",
            requires_human=False,
        )

    def cancel_pending_approval(self, actor_id):
        self._pending = False

    def save(self):
        pass


def make_client(tmp_path):
    config = Settings(
        _env_file=None,
        service_api_key="test-secret",
        trusted_identity_headers=True,
        refund_approver_ids=["actor-1"],
        approver_api_key="approval-secret",
        admin_api_key="admin-secret",
        api_session_db_path=str(tmp_path / "sessions.sqlite"),
        langgraph_checkpoint_path=str(tmp_path / "checkpoints.sqlite"),
        memory_enabled=False,
    )
    return TestClient(create_app(config, FakeAgent))


def headers(user="user-xiaoming", approval=False, admin=False):
    result = {
        "X-API-Key": "test-secret",
        "X-Tenant-ID": "tenant-a",
        "X-User-ID": user,
        "X-Actor-ID": "actor-1",
    }
    if approval:
        result["X-Approval-Key"] = "approval-secret"
    if admin:
        result["X-Admin-Key"] = "admin-secret"
    return result


def test_auth_session_isolation_and_sse(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post("/sessions", headers={**headers(), "X-API-Key": "bad"}).status_code == 401
        created = client.post("/sessions", headers=headers())
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        assert client.post(
            f"/sessions/{session_id}/messages",
            headers=headers("other-user"), json={"text": "你好"},
        ).status_code == 403

        response = client.post(
            f"/sessions/{session_id}/messages",
            headers=headers(), json={"text": "你好"},
        )
        assert response.status_code == 200
        events = client.get(
            f"/sessions/{session_id}/events?limit=2&timeout_seconds=1",
            headers=headers(),
        )
        assert "event: model_started" in events.text
        assert "event: token_delta" in events.text


def test_refund_approval_requires_role(tmp_path):
    with make_client(tmp_path) as client:
        session_id = client.post("/sessions", headers=headers()).json()["session_id"]
        client.post(
            f"/sessions/{session_id}/messages",
            headers={**headers(), "X-Request-ID": "refund-run"},
            json={"text": "我要退款"},
        )
        assert client.get("/runs/refund-run", headers=headers()).json()["status"] == "pending"
        denied = client.post(
            f"/sessions/{session_id}/approval",
            headers=headers(), json={"decision": "approve"},
        )
        assert denied.status_code == 403
        approved = client.post(
            f"/sessions/{session_id}/approval",
            headers=headers(approval=True), json={"decision": "approve"},
        )
        assert approved.status_code == 200
        assert approved.json()["pending_approval"] is False
        assert client.get("/runs/refund-run", headers=headers()).json()["status"] == "completed"


def test_sse_cursor_replays_after_api_restart(tmp_path):
    with make_client(tmp_path) as client:
        session_id = client.post("/sessions", headers=headers()).json()["session_id"]
        client.post(
            f"/sessions/{session_id}/messages",
            headers=headers(), json={"text": "你好"},
        )
    with make_client(tmp_path) as restarted:
        events = restarted.get(
            f"/sessions/{session_id}/events?after=1&limit=1&timeout_seconds=1",
            headers=headers(),
        )
        assert "id: 2" in events.text
        assert "event: token_delta" in events.text


def test_cancel_endpoint_is_owner_scoped(tmp_path):
    with make_client(tmp_path) as client:
        session_id = client.post("/sessions", headers=headers()).json()["session_id"]
        response = client.post(
            f"/sessions/{session_id}/messages",
            headers={**headers(), "X-Request-ID": "run-1"},
            json={"text": "你好"},
        )
        assert response.status_code == 200
        forbidden = client.post("/runs/run-1/cancel", headers=headers("other-user"))
        assert forbidden.status_code == 403
        completed = client.post("/runs/run-1/cancel", headers=headers())
        assert completed.json()["status"] == "completed"

        client.app.state.session_manager.registry.create_run(
            "run-active", session_id, "tenant-a", "user-xiaoming"
        )
        cancelling = client.post("/runs/run-active/cancel", headers=headers())
        assert cancelling.status_code == 202
        assert cancelling.json()["status"] == "cancelling"
        status_response = client.get("/runs/run-active", headers=headers()).json()
        assert status_response["status"] == "running"
        assert status_response["cancel_requested"] is True


def test_sse_last_event_id_and_invalid_cursor(tmp_path):
    with make_client(tmp_path) as client:
        sid = client.post("/sessions", headers=headers()).json()["session_id"]
        client.post(f"/sessions/{sid}/messages", headers=headers(), json={"text": "hi"})
        url = f"/sessions/{sid}/events?limit=1&timeout_seconds=.1"
        result = client.get(url, headers={**headers(), "Last-Event-ID": "1"})
        assert "id: 2" in result.text and "model_started" not in result.text
        assert client.get(url, headers={**headers(), "Last-Event-ID": "bad"}).status_code == 422
        broker = client.app.state.session_manager.sessions[sid].broker
        broker.max_events = 1
        broker.publish({"type": "test"})
        assert client.get(url, headers={**headers(), "Last-Event-ID": "1"}).status_code == 410


def test_run_approval_replay_reject_and_pending_cancel(tmp_path):
    with make_client(tmp_path) as client:
        sid = client.post("/sessions", headers=headers()).json()["session_id"]
        for index, action in enumerate(("approve", "reject", "cancel")):
            rid = f"decision-{index}"
            client.post(f"/sessions/{sid}/messages", headers={**headers(), "X-Request-ID": rid}, json={"text": "退款"})
            result = client.post(f"/runs/{rid}/{action}", headers=headers(approval=True))
            assert result.status_code in (200, 202)
            assert client.post(f"/runs/{rid}/approve", headers=headers(approval=True)).status_code == 409
            assert client.get(f"/runs/{rid}", headers=headers()).json()["status"] == ("cancelled" if action == "cancel" else "completed")


def test_login_token_binds_identity_and_rejects_expiry(tmp_path):
    import time
    client = make_client(tmp_path)
    config = client.app.state.session_manager.config
    config.trusted_identity_headers = False
    config.auth_tokens = {
        "user-token": {"tenant_id": "tenant-a", "user_id": "user-xiaoming", "actor_id": "owner", "expires_at": time.time() + 60},
        "expired": {"tenant_id": "tenant-a", "user_id": "user-xiaoming", "actor_id": "owner", "expires_at": 1},
    }
    with client:
        assert client.post("/sessions", headers=headers()).status_code == 401
        assert client.post("/sessions", headers={"Authorization": "Bearer expired"}).status_code == 401
        token_headers = {"Authorization": "Bearer user-token"}
        sid = client.post("/sessions", headers=token_headers).json()["session_id"]
        assert client.post(f"/sessions/{sid}/messages", headers={**headers("imposter"), **token_headers}, json={"text": "hi"}).status_code == 200


def test_client_reconnects_with_last_received_cursor():
    import httpx
    from app.api_client import session_events
    calls = []
    def handler(request):
        calls.append(request.headers["Last-Event-ID"])
        sequence = len(calls)
        return httpx.Response(200, text=f'id: {sequence}\ndata: {{"sequence": {sequence}, "type": "token_delta"}}\n\n')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stream = session_events("http://test", "session", "token", client=client)
        assert next(stream)["sequence"] == 1
        assert next(stream)["sequence"] == 2
        stream.close()
    assert calls == ["0", "1"]


def test_message_disconnect_keeps_background_run_and_capacity(tmp_path):
    import asyncio
    import threading
    import httpx

    started = threading.Event()
    release = threading.Event()
    class BlockingAgent(FakeAgent):
        def chat(self, *args, **kwargs):
            started.set()
            assert release.wait(2)
            return super().chat(*args, **kwargs)

    async def scenario():
        config = Settings(_env_file=None, trusted_identity_headers=True, service_api_key="test-secret",
                          api_session_db_path=str(tmp_path / "sessions.sqlite"),
                          langgraph_checkpoint_path=str(tmp_path / "checkpoints.sqlite"), max_concurrent_runs=1)
        app = create_app(config, BlockingAgent)
        manager = app.state.session_manager
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                sid = (await client.post("/sessions", headers=headers())).json()["session_id"]
                request = asyncio.create_task(client.post(f"/sessions/{sid}/messages", headers={**headers(), "X-Request-ID": "disconnect"}, json={"text": "hi"}))
                assert await asyncio.to_thread(started.wait, 1)
                request.cancel()
                try:
                    await request
                except asyncio.CancelledError:
                    pass
                assert not manager.run_slots.acquire(blocking=False)
                release.set()
                await asyncio.gather(*manager.tasks)
                assert manager.registry.get_run("disconnect", "tenant-a", "user-xiaoming")["status"] == "completed"
                assert manager.run_slots.acquire(blocking=False)
                manager.run_slots.release()
        finally:
            release.set()
            manager.close()
    asyncio.run(scenario())
