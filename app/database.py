"""Small SQL dialect boundary and PostgreSQL advisory locks.

Application SQL uses positional '?' parameters. Only our fixed application SQL is
translated; values always remain bound parameters. PostgreSQL connections use
explicit, short transactions and a finite connect timeout.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager

import psycopg


class DatabaseBusy(RuntimeError):
    pass


def lock_id(namespace: str, key: str) -> int:
    return int.from_bytes(hashlib.sha256(f"ecom:{namespace}:{key}".encode()).digest()[:8], "big", signed=True)


def pg_connect(url: str):
    return psycopg.connect(url, autocommit=True, connect_timeout=5)


class PostgresConnection:
    """DB-API subset used by the existing SQLite repositories."""
    def __init__(self, url: str):
        self.raw = pg_connect(url)

    def execute(self, query: str, params=()):
        if query == "BEGIN IMMEDIATE":
            query = "BEGIN"
        if "INSERT OR IGNORE INTO" in query:
            query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
        return self.raw.execute(query.replace("?", "%s"), params)

    def executescript(self, script: str):
        script = script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        script = script.replace("expires_at REAL", "expires_at DOUBLE PRECISION")
        # DDL can race at first boot even with IF NOT EXISTS. Serialize it.
        with self.raw.transaction():
            self.raw.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id("schema", "application"),))
            for statement in script.split(";"):
                if statement.strip():
                    self.raw.execute(statement)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        self.execute("BEGIN")
        return self

    def __exit__(self, kind, value, traceback):
        self.rollback() if kind else self.commit()


def refund_transaction_lock(connection, tenant: str, user: str):
    if isinstance(connection, PostgresConnection):
        connection.execute("SELECT pg_advisory_xact_lock(?)", (lock_id("refund-user", f"{tenant}:{user}"),))


@contextmanager
def advisory_lock(url: str, namespace: str, key: str, *, wait: bool = False):
    with pg_connect(url) as connection:
        identifier = lock_id(namespace, key)
        if wait:
            connection.execute("SET lock_timeout = '10s'")
            connection.execute("SELECT pg_advisory_lock(%s)", (identifier,))
        elif not connection.execute("SELECT pg_try_advisory_lock(%s)", (identifier,)).fetchone()[0]:
            raise DatabaseBusy("该会话正在另一实例执行，请稍后重试")
        try:
            yield connection
        finally:
            if not connection.closed:
                connection.execute("SELECT pg_advisory_unlock(%s)", (identifier,))


class PostgresRunSlots:
    """Cluster-wide capacity, released automatically on process/connection loss."""
    def __init__(self, url: str, limit: int):
        self.url, self.limit = url, limit
        self._held = []
        self._lock = threading.Lock()

    def acquire(self, blocking=False):
        connection = pg_connect(self.url)
        for slot in range(self.limit):
            key = lock_id("run-slot", str(slot))
            if connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                with self._lock:
                    self._held.append(connection)
                return True
        connection.close()
        return False

    def release(self):
        with self._lock:
            connection = self._held.pop()
        connection.close()
