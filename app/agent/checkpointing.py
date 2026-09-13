"""Checkpoint backend factory plus thread ownership and retention metadata."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from app.database import advisory_lock, DatabaseBusy
from datetime import UTC, datetime, timedelta
from pathlib import Path


class CheckpointStore:
    def __init__(
        self,
        *,
        backend: str,
        sqlite_path: str,
        database_url: str,
    ):
        self.backend = backend
        self.database_url = database_url
        if backend == "sqlite":
            path = Path(sqlite_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
            from langgraph.checkpoint.sqlite import SqliteSaver
            self.saver = SqliteSaver(self.connection)
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_threads (
                    thread_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )"""
            )
            columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(agent_threads)")
            }
            if "status" not in columns:
                self.connection.execute(
                    "ALTER TABLE agent_threads ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
                )
            self.connection.commit()
        elif backend == "postgres":
            if not database_url:
                raise ValueError("CHECKPOINT_BACKEND=postgres 时必须配置 DATABASE_URL")
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
            from langgraph.checkpoint.postgres import PostgresSaver
            self.connection = ConnectionPool(
                conninfo=database_url,
                min_size=1,
                open=True,
                max_size=10,
                timeout=5,
                kwargs={
                    "autocommit": True,
                    "connect_timeout": 5,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            try:
                with advisory_lock(database_url, "schema", "checkpoint", wait=True):
                    self.saver = PostgresSaver(self.connection)
                    self.connection.wait(timeout=5)
                    self.saver.setup()
                    with self.connection.connection() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            """CREATE TABLE IF NOT EXISTS agent_threads (
                                thread_id TEXT PRIMARY KEY,
                                tenant_id TEXT NOT NULL,
                                user_id TEXT NOT NULL,
                                updated_at TIMESTAMPTZ NOT NULL,
                                status TEXT NOT NULL DEFAULT 'active'
                            )"""
                        )
                        cursor.execute(
                            "ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'"
                        )
            except Exception:
                self.connection.close()
                raise
        else:
            raise ValueError("CHECKPOINT_BACKEND 必须是 'sqlite' 或 'postgres'")

    def touch(self, thread_id: str, tenant_id: str, user_id: str) -> None:
        self.assert_owner(thread_id, tenant_id, user_id)
        now = datetime.now(UTC)
        if self.backend == "sqlite":
            with self._metadata_lock():
                self.connection.execute(
                    """INSERT INTO agent_threads(thread_id, tenant_id, user_id, updated_at, status)
                    VALUES (?, ?, ?, ?, 'active')
                    ON CONFLICT(thread_id) DO UPDATE SET
                        updated_at=excluded.updated_at,
                        status='active'
                    WHERE agent_threads.tenant_id = excluded.tenant_id
                      AND agent_threads.user_id = excluded.user_id""",
                    (thread_id, tenant_id, user_id, now.isoformat()),
                )
                self.connection.commit()
        else:
            with self.connection.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO agent_threads(thread_id, tenant_id, user_id, updated_at, status)
                    VALUES (%s, %s, %s, %s, 'active')
                    ON CONFLICT(thread_id) DO UPDATE SET
                        updated_at=EXCLUDED.updated_at,
                        status='active'
                    WHERE agent_threads.tenant_id = EXCLUDED.tenant_id
                      AND agent_threads.user_id = EXCLUDED.user_id""",
                    (thread_id, tenant_id, user_id, now),
                )

        self.assert_owner(thread_id, tenant_id, user_id)

    def assert_owner(self, thread_id: str, tenant_id: str, user_id: str) -> None:
        placeholder = "?" if self.backend == "sqlite" else "%s"
        query = (
            "SELECT tenant_id, user_id FROM agent_threads WHERE thread_id = "
            + placeholder
        )
        if self.backend == "sqlite":
            with self._metadata_lock():
                row = self.connection.execute(query, (thread_id,)).fetchone()
        else:
            with self.connection.connection() as connection, connection.cursor() as cursor:
                cursor.execute(query, (thread_id,))
                row = cursor.fetchone()
        if row is None:
            return
        owner_tenant = row[0] if self.backend == "sqlite" else row["tenant_id"]
        owner_user = row[1] if self.backend == "sqlite" else row["user_id"]
        if owner_tenant != tenant_id or owner_user != user_id:
            raise PermissionError("checkpoint 不属于当前租户或用户")

    def delete_thread(self, thread_id: str) -> None:
        self.saver.delete_thread(thread_id)
        placeholder = "?" if self.backend == "sqlite" else "%s"
        query = "DELETE FROM agent_threads WHERE thread_id = " + placeholder
        if self.backend == "sqlite":
            with self._metadata_lock():
                self.connection.execute(query, (thread_id,))
                self.connection.commit()
        else:
            with self.connection.connection() as connection, connection.cursor() as cursor:
                cursor.execute(query, (thread_id,))

    def mark_status(self, thread_id: str, status: str) -> None:
        if status not in {"active", "pending", "completed", "failed"}:
            raise ValueError("invalid checkpoint thread status")
        placeholder = "?" if self.backend == "sqlite" else "%s"
        query = "UPDATE agent_threads SET status = " + placeholder + " WHERE thread_id = " + placeholder
        if self.backend == "sqlite":
            with self._metadata_lock():
                self.connection.execute(query, (status, thread_id))
                self.connection.commit()
        else:
            with self.connection.connection() as connection, connection.cursor() as cursor:
                cursor.execute(query, (status, thread_id))

    def cleanup(self, retention_days: int) -> int:
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        placeholder = "?" if self.backend == "sqlite" else "%s"
        query = (
            "SELECT thread_id FROM agent_threads WHERE status = 'completed' AND updated_at < "
            + placeholder
        )
        if self.backend == "sqlite":
            with self._metadata_lock():
                rows = self.connection.execute(query, (cutoff.isoformat(),)).fetchall()
        else:
            with self.connection.connection() as connection, connection.cursor() as cursor:
                cursor.execute(query, (cutoff,))
                rows = cursor.fetchall()
        thread_ids = [row[0] if self.backend == "sqlite" else row["thread_id"] for row in rows]
        deleted = 0
        for thread_id in thread_ids:
            if self.backend == "sqlite":
                self.delete_thread(thread_id)
                deleted += 1
                continue
            try:
                with advisory_lock(self.database_url, "checkpoint-thread", thread_id) as connection:
                    with connection.transaction():
                        row = connection.execute(
                            "SELECT status, updated_at FROM agent_threads WHERE thread_id = %s FOR UPDATE", (thread_id,)
                        ).fetchone()
                        if not row or row[0] != "completed" or row[1] >= cutoff:
                            continue
                        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "agent_threads"):
                            connection.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
                        deleted += 1
            except DatabaseBusy:
                continue
        return deleted

    def thread_guard(self, thread_id):
        return advisory_lock(self.database_url, "checkpoint-thread", thread_id) if self.backend == "postgres" else nullcontext()

    def close(self) -> None:
        self.connection.close()

    def _metadata_lock(self):
        return self.saver.lock if self.backend == "sqlite" else nullcontext()
