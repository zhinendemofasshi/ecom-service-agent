"""Offline, transactional SQLite -> PostgreSQL migration.

Stop API/CLI writers first. Without --apply only inventory is printed. Original
SQLite/JSON files are preserved; a consistent SQLite backup is made before import.
An unchanged source can be applied repeatedly. Changed source or nonempty target
is rejected rather than overwriting newer PostgreSQL state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from psycopg import sql
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.checkpointing import CheckpointStore
from app.agent.document_store import connect as document_connect
from app.agent.tools.refund import _connect as refund_connect
from app.config.settings import Settings
from app.database import advisory_lock, pg_connect


TABLES = {
    "api": ("api_sessions", "api_runs", "api_events", "human_handoffs"),
    "refund": ("refund_approvals", "refund_requests", "refund_audit"),
    "checkpoint": ("agent_threads",),
}


def initialize(url: str):
    from app.session_registry import SessionRegistry
    registry = SessionRegistry("unused", url)
    registry.close()
    refund_connect(url).close()
    document_connect(url).close()
    store = CheckpointStore(backend="postgres", sqlite_path="unused", database_url=url)
    store.close()
    with pg_connect(url) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS storage_migrations (
            source_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            counts TEXT NOT NULL, completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")


def migrate(config: Settings, *, apply=False, backup_dir: str | None = None) -> dict:
    sources = {"api": Path(config.api_session_db_path), "refund": Path(config.refund_db_path),
               "checkpoint": Path(config.langgraph_checkpoint_path)}
    connections = {}
    documents = []
    counts = {}
    digest = hashlib.sha256()
    with TemporaryDirectory(prefix="ecom-pg-migration-") as temporary:
        snapshots = {}
        try:
            for kind, source in sources.items():
                if not source.exists():
                    continue
                snapshot = Path(temporary) / f"{kind}.sqlite"
                with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as original:
                    destination = sqlite3.connect(snapshot)
                    original.backup(destination)
                    destination.close()
                connection = sqlite3.connect(snapshot, check_same_thread=False)
                connections[kind] = connection
                snapshots[kind] = snapshot
                for line in connection.iterdump():
                    digest.update(line.encode())
                present = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in TABLES[kind]:
                    if table in present:
                        counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if kind == "checkpoint" and "checkpoints" in present:
                    counts["checkpoints"] = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]

            def add_document(namespace, key, path):
                if not path.exists():
                    return
                data = path.read_bytes()
                payload = json.loads(data)
                digest.update(str(path.resolve()).encode() + data)
                documents.append((namespace, key, payload, path))

            if "api" in connections:
                for tenant, user, session_path in connections["api"].execute("SELECT tenant_id, user_id, session_path FROM api_sessions"):
                    path = Path(session_path).resolve()
                    add_document("session", json.dumps([tenant, user, str(path)]), path)
            cli_path = Path(config.session_path).resolve()
            add_document("session", json.dumps(["local", config.memory_user_id, str(cli_path)]), cli_path)
            owners = {("local", config.memory_user_id)}
            if "checkpoint" in connections:
                owners.update(connections["checkpoint"].execute("SELECT DISTINCT tenant_id, user_id FROM agent_threads"))
            for tenant, user in sorted(owners):
                directory = Path(config.memory_dir)
                if tenant != "local":
                    directory /= hashlib.sha256(tenant.encode()).hexdigest()
                add_document("memory", json.dumps([tenant, user]), directory / f"{user}.json")
            counts["agent_documents"] = len(documents)
            fingerprint = digest.hexdigest()
            source_key = hashlib.sha256(json.dumps([str(p.resolve()) for p in sources.values()]).encode()).hexdigest()
            if not apply:
                return {"mode": "plan", "counts": counts, "fingerprint": fingerprint}
            if not config.database_url:
                raise ValueError("DATABASE_URL is required")
            initialize(config.database_url)
            backup = Path(backup_dir or (Path("app/sessions/backups") / datetime.now(UTC).strftime("postgres-%Y%m%dT%H%M%S%f")))
            backup.mkdir(parents=True, exist_ok=False)
            for kind, snapshot in snapshots.items():
                shutil.copy2(snapshot, backup / f"{kind}.sqlite")
            for index, (_, _, _, path) in enumerate(documents):
                shutil.copy2(path, backup / f"document-{index}.json")
            (backup / "manifest.json").write_text(json.dumps({
                "sources": {k: str(v.resolve()) for k, v in sources.items()},
                "documents": [{"namespace": n, "key": k, "path": str(p)} for n, k, _, p in documents],
                "fingerprint": fingerprint, "counts": counts,
            }, ensure_ascii=False, indent=2))
            with advisory_lock(config.database_url, "migration", "sqlite-import", wait=True) as target:
                with target.transaction():
                    existing = target.execute("SELECT fingerprint, counts FROM storage_migrations WHERE source_key=%s", (source_key,)).fetchone()
                    if existing:
                        if existing[0] != fingerprint:
                            raise RuntimeError("Source changed after migration; refusing to overwrite PostgreSQL state")
                        return {"mode": "already_applied", "counts": json.loads(existing[1]), "backup": str(backup)}
                    for table in (*TABLES["api"], *TABLES["refund"], "agent_threads", "checkpoints", "agent_documents"):
                        if target.execute(sql.SQL("SELECT EXISTS(SELECT 1 FROM {})").format(sql.Identifier(table))).fetchone()[0]:
                            raise RuntimeError(f"Target table {table} is not empty; refusing to overwrite")
                    for kind, tables in TABLES.items():
                        source = connections.get(kind)
                        if source is None:
                            continue
                        present = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        for table in tables:
                            if table not in present:
                                continue
                            cursor = source.execute(f"SELECT * FROM {table}")
                            columns = [d[0] for d in cursor.description]
                            query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                                sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
                                sql.SQL(",").join(sql.Placeholder() for _ in columns))
                            for record in cursor:
                                row = list(record)
                                if table == "api_sessions":
                                    row[columns.index("session_path")] = str(Path(row[columns.index("session_path")]).resolve())
                                if table == "api_runs" and row[columns.index("status")] == "running":
                                    row[columns.index("status")] = "failed"  # never replay an uncertain write
                                target.execute(query, row)
                    if "checkpoint" in connections and counts.get("checkpoints"):
                        source_saver = SqliteSaver(connections["checkpoint"])
                        target_saver = PostgresSaver(target)
                        for item in reversed(list(source_saver.list(None))):
                            parent = item.parent_config or {"configurable": {
                                "thread_id": item.config["configurable"]["thread_id"],
                                "checkpoint_ns": item.config["configurable"].get("checkpoint_ns", ""),
                            }}
                            target_saver.put(parent, item.checkpoint, item.metadata, item.checkpoint.get("channel_versions", {}))
                            pending = defaultdict(list)
                            for task_id, channel, value in item.pending_writes or []:
                                pending[task_id].append((channel, value))
                            for task_id, writes in pending.items():
                                target_saver.put_writes(item.config, writes, task_id)
                    for namespace, key, payload, _ in documents:
                        target.execute("INSERT INTO agent_documents(namespace,document_key,payload) VALUES (%s,%s,%s)",
                                       (namespace, key, json.dumps(payload, ensure_ascii=False)))
                    target.execute("SELECT setval(pg_get_serial_sequence('refund_audit','id'), COALESCE(MAX(id), 1), COUNT(*) > 0) FROM refund_audit")
                    target.execute("INSERT INTO storage_migrations(source_key,fingerprint,counts) VALUES (%s,%s,%s)",
                                   (source_key, fingerprint, json.dumps(counts)))
            return {"mode": "applied", "counts": counts, "backup": str(backup)}
        finally:
            for connection in connections.values():
                connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Import after stopping all API/CLI writers")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    print(json.dumps(migrate(Settings(), apply=args.apply, backup_dir=args.backup_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
