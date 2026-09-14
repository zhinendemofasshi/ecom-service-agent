"""Durable API sessions, runs and SSE event replay on SQLite or PostgreSQL."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.database import PostgresConnection, advisory_lock, DatabaseBusy


class SessionRegistry:
    def __init__(self, db_path: str, database_url: str = ""):
        self.database_url = database_url
        if database_url:
            self.connection = PostgresConnection(database_url)
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS api_sessions (
                session_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_runs (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, sequence),
                FOREIGN KEY(session_id) REFERENCES api_sessions(session_id)
            );
        """)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS human_handoffs (
                request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS api_runs_session_status ON api_runs(session_id, status);
            CREATE INDEX IF NOT EXISTS api_runs_status_updated ON api_runs(status, updated_at);
        """)
        self.connection.commit()

    def create(self, tenant_id: str, user_id: str, session_path: str) -> str:
        session_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self.lock:
            self.connection.execute(
                "INSERT INTO api_sessions VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, tenant_id, user_id, session_path, now, now),
            )
            self.connection.commit()
        return session_id

    def get(self, session_id: str, tenant_id: str, user_id: str) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT tenant_id, user_id, session_path FROM api_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise KeyError(session_id)
            if row[0] != tenant_id or row[1] != user_id:
                raise PermissionError(session_id)
            self.connection.execute(
                "UPDATE api_sessions SET updated_at = ? WHERE session_id = ?",
                (datetime.now(UTC).isoformat(), session_id),
            )
            self.connection.commit()
            return row[2]

    def append_event(self, session_id: str, event: dict, max_events: int) -> dict:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                if self.database_url:
                    self.connection.execute("SELECT session_id FROM api_sessions WHERE session_id = ? FOR UPDATE", (session_id,))
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM api_events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                sequence = int(row[0])
                enriched = {"sequence": sequence, **event}
                self.connection.execute(
                    "INSERT INTO api_events VALUES (?, ?, ?, ?)",
                    (session_id, sequence, json.dumps(enriched, ensure_ascii=False), event["timestamp"]),
                )
                cutoff = max(0, sequence - max_events)
                self.connection.execute(
                    "DELETE FROM api_events WHERE session_id = ? AND sequence <= ?",
                    (session_id, cutoff),
                )
                if event.get('type')=='handoff_required' and event.get('request_id'):
                    self.connection.execute("INSERT OR IGNORE INTO human_handoffs(request_id,session_id,payload,status,created_at) VALUES (?,?,?,'open',?)",
                        (event['request_id'],session_id,json.dumps(enriched,ensure_ascii=False),event['timestamp']))
                self.connection.commit()
                return enriched
            except Exception:
                self.connection.rollback()
                raise

    def events_after(self, session_id: str, sequence: int) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                """SELECT event_json FROM api_events
                WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT 1000""",
                (session_id, sequence),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create_run(self, request_id: str, session_id: str, tenant_id: str, user_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.lock:
            self.connection.execute(
                "INSERT INTO api_runs VALUES (?, ?, ?, ?, 'running', 0, ?, ?)",
                (request_id, session_id, tenant_id, user_id, now, now),
            )
            self.connection.commit()

    def finish_run(self, request_id: str, status_value: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE api_runs SET status = ?, updated_at = ? WHERE request_id = ?",
                (status_value, datetime.now(UTC).isoformat(), request_id),
            )
            self.connection.commit()

    def finish_pending_runs(self, session_id: str, status_value: str) -> None:
        with self.lock:
            self.connection.execute(
                """UPDATE api_runs SET status = ?, updated_at = ?
                WHERE session_id = ? AND status = 'pending'""",
                (status_value, datetime.now(UTC).isoformat(), session_id),
            )
            self.connection.commit()

    def is_cancel_requested(self, request_id: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT cancel_requested FROM api_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
        return bool(row and row[0])

    def cancel_run(self, request_id: str, tenant_id: str, user_id: str) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT tenant_id, user_id, status FROM api_runs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if not row:
                raise KeyError(request_id)
            if row[0] != tenant_id or row[1] != user_id:
                raise PermissionError(request_id)
            if row[2] != "running":
                return row[2]
            self.connection.execute(
                "UPDATE api_runs SET cancel_requested = 1, updated_at = ? WHERE request_id = ?",
                (datetime.now(UTC).isoformat(), request_id),
            )
            self.connection.commit()
            return "cancelling"

    def get_run(self, request_id: str, tenant_id: str, user_id: str) -> dict:
        with self.lock:
            row = self.connection.execute(
                """SELECT session_id, tenant_id, user_id, status, cancel_requested,
                created_at, updated_at FROM api_runs WHERE request_id = ?""",
                (request_id,),
            ).fetchone()
        if not row:
            raise KeyError(request_id)
        if row[1] != tenant_id or row[2] != user_id:
            raise PermissionError(request_id)
        return {
            "request_id": request_id,
            "session_id": row[0],
            "status": row[3],
            "cancel_requested": bool(row[4]),
            "created_at": row[5],
            "updated_at": row[6],
        }

    def recover_abandoned(self, age_seconds: int) -> int:
        if not self.database_url:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        with self.lock:
            rows = self.connection.execute(
                "SELECT request_id, session_id, tenant_id, user_id FROM api_runs WHERE status='running' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
        recovered = 0
        for request_id, session_id, tenant, user in rows:
            try:
                with advisory_lock(self.database_url, "api-user", f"{tenant}:{user}"):
                    with self.lock:
                        changed = self.connection.execute(
                            "UPDATE api_runs SET status='failed', updated_at=? WHERE request_id=? AND status='running' AND updated_at < ?",
                            (datetime.now(UTC).isoformat(), request_id, cutoff),
                        ).rowcount
                    if changed:
                        self.append_event(session_id, {"type":"run_failed", "request_id":request_id,
                            "timestamp":datetime.now(UTC).isoformat(), "error":"执行进程已退出；请核对业务幂等结果"}, 2000)
                        recovered += 1
            except DatabaseBusy:
                continue  # A long-running owner still holds the lock.
        return recovered

    def close(self) -> None:
        self.connection.close()


