"""Endpoints that back the React console: demo login, /me, session list, static hosting."""
from fastapi.testclient import TestClient

from app.api import create_app
from app.config.settings import Settings
from tests.test_api import FakeAgent


def make_client(tmp_path, **overrides):
    config = Settings(
        _env_file=None,
        api_session_db_path=str(tmp_path / "sessions.sqlite"),
        langgraph_checkpoint_path=str(tmp_path / "checkpoints.sqlite"),
        memory_enabled=False,
        console_dist_dir=str(tmp_path / "dist"),
        **overrides,
    )
    return TestClient(create_app(config, FakeAgent))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_demo_login_disabled_by_default(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/auth/config").json()["demo_login"] is False
        assert client.post("/auth/demo-login", json={"role": "customer"}).status_code == 404


def test_demo_login_issues_working_token_and_logout_revokes(tmp_path):
    with make_client(tmp_path, demo_login_enabled=True) as client:
        login = client.post("/auth/demo-login", json={"role": "customer"}).json()
        assert login["identity"]["user_id"] == "user-xiaoming"
        assert login["identity"]["can_approve"] is False
        me = client.get("/me", headers=bearer(login["token"]))
        assert me.status_code == 200 and me.json()["actor_id"] == "user-xiaoming"

        assert client.post("/auth/logout", headers=bearer(login["token"])).status_code == 204
        assert client.get("/me", headers=bearer(login["token"])).status_code == 401


def test_demo_approver_requires_allowlist(tmp_path):
    with make_client(tmp_path, demo_login_enabled=True) as client:
        assert client.post("/auth/demo-login", json={"role": "approver"}).status_code == 409
    with make_client(tmp_path, demo_login_enabled=True, refund_approver_ids=["demo-reviewer"]) as client:
        login = client.post("/auth/demo-login", json={"role": "approver"}).json()
        assert login["identity"]["can_approve"] is True
        assert login["identity"]["actor_id"] == "demo-reviewer"


def test_session_list_is_owner_scoped_and_titled(tmp_path):
    with make_client(tmp_path, demo_login_enabled=True) as client:
        token = client.post("/auth/demo-login", json={}).json()["token"]
        session_id = client.post("/sessions", headers=bearer(token)).json()["session_id"]
        assert client.get("/sessions", headers=bearer(token)).json()["items"][0]["title"] == "新会话"

        client.post(f"/sessions/{session_id}/messages", json={"text": "你好\n  在吗"}, headers=bearer(token))
        client.post(f"/sessions/{session_id}/messages", json={"text": "第二条"}, headers=bearer(token))
        items = client.get("/sessions", headers=bearer(token)).json()["items"]
        assert [(i["session_id"], i["title"]) for i in items] == [(session_id, "你好 在吗")]

        assert client.get("/sessions").status_code == 401


def test_console_static_hosting(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    with make_client(tmp_path) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/console/"
        assert "root" in client.get("/console/chat/abc").text
        assert client.get("/console/assets/app.js").text == "console.log(1)"
        assert "nope" not in client.get("/console/../secret.txt").text
