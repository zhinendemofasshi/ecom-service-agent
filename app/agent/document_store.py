"""Shared conversation and memory documents for PostgreSQL deployments."""
from __future__ import annotations

import json

from app.database import PostgresConnection


DDL = """
CREATE TABLE IF NOT EXISTS agent_documents (
    namespace TEXT NOT NULL,
    document_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(namespace, document_key)
);
"""


def connect(url):
    connection = PostgresConnection(url)
    connection.executescript(DDL)
    return connection


def load(url, namespace, key):
    connection = connect(url)
    try:
        row = connection.execute("SELECT payload FROM agent_documents WHERE namespace = ? AND document_key = ?", (namespace, key)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        connection.close()


def save(url, namespace, key, payload):
    connection = connect(url)
    try:
        connection.execute("""INSERT INTO agent_documents(namespace, document_key, payload) VALUES (?, ?, ?)
            ON CONFLICT(namespace, document_key) DO UPDATE SET payload=excluded.payload, updated_at=now()""",
            (namespace, key, json.dumps(payload, ensure_ascii=False)))
    finally:
        connection.close()


def delete(url, namespace, key):
    connection = connect(url)
    try:
        connection.execute("DELETE FROM agent_documents WHERE namespace = ? AND document_key = ?", (namespace, key))
    finally:
        connection.close()


def cleanup(url, memory_days, session_days):
    from datetime import UTC,datetime,timedelta
    from app.database import advisory_lock,DatabaseBusy
    connection=connect(url)
    deleted=0
    try:
        for namespace,days in [('memory',memory_days),('session',session_days)]:
            cutoff=datetime.now(UTC)-timedelta(days=max(1,days))
            rows=connection.execute('SELECT document_key,payload FROM agent_documents WHERE namespace=? AND updated_at < ?', (namespace,cutoff)).fetchall()
            for key,raw in rows:
                # Only archive documents which have already been imported into Graph.
                if namespace=='session' and not json.loads(raw).get('migrated_to_graph'):
                    continue
                identity=json.loads(key)
                try:
                    with advisory_lock(url,'api-user',f'{identity[0]}:{identity[1]}'):
                        deleted+=connection.execute('DELETE FROM agent_documents WHERE namespace=? AND document_key=? AND updated_at < ?', (namespace,key,cutoff)).rowcount
                except DatabaseBusy:
                    continue
        return deleted
    finally:
        connection.close()
