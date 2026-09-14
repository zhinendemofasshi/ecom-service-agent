"""Refund application with ownership checks, idempotency, and audit."""

from __future__ import annotations

import json
import sqlite3
import time
import math
from datetime import UTC, datetime
from pathlib import Path

from app.agent.tools.mock_data import ORDERS
from app.database import PostgresConnection, refund_transaction_lock


def _connect(db_path: str) -> sqlite3.Connection | PostgresConnection:
    if db_path.startswith(("postgresql:", "postgres:", "dbname=", "host=", "service=", "user=")):
        connection = PostgresConnection(db_path)
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS refund_approvals (
            idempotency_key TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refund_requests (
            idempotency_key TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(tenant_id, order_id)
        );
        CREATE TABLE IF NOT EXISTS refund_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    if isinstance(connection, PostgresConnection):
        connection.execute("ALTER TABLE refund_approvals ADD COLUMN IF NOT EXISTS request_id TEXT NOT NULL DEFAULT ''")
    elif 'request_id' not in {r[1] for r in connection.execute('PRAGMA table_info(refund_approvals)')}:
        connection.execute("ALTER TABLE refund_approvals ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
        connection.commit()
    return connection


def _apply_refund(
    order_id: str,
    reason: str,
    *,
    tenant_id: str,
    user_id: str,
    idempotency_key: str,
    approved_by: str,
    db_path: str,
    approver_ids: list[str] | None = None,
    max_refund_amount: float = 10_000.0,
    max_refunds_per_user_per_day: int = 5,
) -> dict:
    """Apply a refund after deterministic server-side authorization checks."""
    if not all((tenant_id, user_id, idempotency_key, approved_by)):
        return {"success": False, "error_code": "missing_context", "error": "退款缺少身份、幂等键或审批人"}
    if not reason.strip():
        return {"success": False, "error_code": "invalid_reason", "error": "退款原因不能为空"}
    order = ORDERS.get(order_id)
    if not order:
        return {"success": False, "error_code": "order_not_found", "error": f"未找到订单 {order_id}"}
    if order.get("user_id") != user_id:
        return {"success": False, "error_code": "forbidden", "error": "订单不属于当前用户"}
    if order.get("tenant_id") != tenant_id:
        return {"success": False, "error_code": "forbidden", "error": "订单不属于当前租户"}

    connection = _connect(db_path)
    now = datetime.now(UTC).isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        refund_transaction_lock(connection, tenant_id, user_id)
        grant = connection.execute(
            "SELECT tenant_id, user_id, order_id, reason, status, actor_id, expires_at FROM refund_approvals WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        from app.config.settings import settings
        authorized_approvers = settings.refund_approver_ids if approver_ids is None else approver_ids
        if (not grant or grant[:4] != (tenant_id, user_id, order_id, reason)
                or grant[4] not in {"approve", "edit", "consumed"}
                or grant[5] != approved_by or approved_by not in authorized_approvers
                or (grant[4] != "consumed" and grant[6] <= time.time())):
            result = {"success": False, "error_code": "approval_invalid", "error": "退款审批无效、已取消或已超时"}
            _audit(connection, idempotency_key, tenant_id, user_id, order_id, "execute", "rejected", {**result, "actor_id": approved_by}, now)
            connection.commit()
            return result
        existing = connection.execute(
            "SELECT tenant_id, user_id, order_id, reason, result_json FROM refund_requests WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing[:4] != (tenant_id, user_id, order_id, reason):
                result = {"success": False, "error_code": "idempotency_conflict", "error": "幂等键已用于其他退款请求"}
            else:
                result = json.loads(existing[4])
                result["idempotent_replay"] = True
            _audit(connection, idempotency_key, tenant_id, user_id, order_id, "replay", "accepted" if result.get("success") else "rejected", result, now)
            connection.commit()
            return result
        if order["status"] == "refund_processing":
            result = {"success": False, "error_code": "already_refunding", "error": "该订单已有退款申请正在处理中"}
            _audit(connection, idempotency_key, tenant_id, user_id, order_id, "create", "rejected", result, now)
            connection.commit()
            return result
        duplicate = connection.execute(
            "SELECT idempotency_key FROM refund_requests WHERE tenant_id = ? AND order_id = ?", (tenant_id, order_id)
        ).fetchone()
        if duplicate:
            result = {"success": False, "error_code": "duplicate_refund", "error": "该订单已经提交过退款申请"}
            _audit(connection, idempotency_key, tenant_id, user_id, order_id, "create", "rejected", result, now)
            connection.commit()
            return result
        if not math.isfinite(float(order["total"])) or not 0 < float(order["total"]) <= max_refund_amount:
            result = {
                "success": False,
                "error_code": "amount_limit_exceeded",
                "error": f"订单金额超过自动退款上限 {max_refund_amount:.2f}",
            }
            _audit(connection, idempotency_key, tenant_id, user_id, order_id, "create", "rejected", result, now)
            connection.commit()
            return result
        day_start = now[:10] + "T00:00:00+00:00"
        daily_count = connection.execute(
            """SELECT COUNT(*) FROM refund_requests
            WHERE tenant_id = ? AND user_id = ? AND created_at >= ?""",
            (tenant_id, user_id, day_start),
        ).fetchone()[0]
        if daily_count >= max_refunds_per_user_per_day:
            result = {
                "success": False,
                "error_code": "daily_limit_exceeded",
                "error": "已达到当前用户每日退款次数上限",
            }
            _audit(connection, idempotency_key, tenant_id, user_id, order_id, "create", "rejected", result, now)
            connection.commit()
            return result
        message = (
            f"订单 {order_id} 尚未发货，已取消并发起退款，预计 1-3 个工作日原路退回。"
            if order["status"] == "pending"
            else f"退款申请已提交。订单 {order_id}，预计 1-3 个工作日内审核完成。"
        )
        result = {"success": True, "order_id": order_id, "status": "submitted", "message": message}
        connection.execute(
            """INSERT INTO refund_requests(
                idempotency_key, tenant_id, user_id, order_id, reason,
                status, result_json, approved_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (idempotency_key, tenant_id, user_id, order_id, reason, "submitted", json.dumps(result, ensure_ascii=False), approved_by, now),
        )
        connection.execute("UPDATE refund_approvals SET status = 'consumed' WHERE idempotency_key = ?", (idempotency_key,))
        _audit(connection, idempotency_key, tenant_id, user_id, order_id, "create", "accepted", {**result, "actor_id": approved_by, "amount": order["total"]}, now)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _audit(connection, key, tenant, user, order, action, outcome, details, now) -> None:
    connection.execute(
        """INSERT INTO refund_audit(
            idempotency_key, tenant_id, user_id, order_id,
            action, outcome, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, tenant, user, order, action, outcome, json.dumps(details, ensure_ascii=False), now),
    )


def register_refund_approval(*, db_path: str, idempotency_key: str, tenant_id: str,
                             user_id: str, order_id: str, reason: str, timeout_seconds: float, request_id: str = "") -> None:
    connection = _connect(db_path)
    try:
        with connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO refund_approvals(idempotency_key,tenant_id,user_id,order_id,reason,status,actor_id,expires_at,request_id) VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?)",
                (idempotency_key, tenant_id, user_id, order_id, reason, time.time() + timeout_seconds, request_id),
            ).rowcount
            if inserted:
                _audit(connection, idempotency_key, tenant_id, user_id, order_id,
                       "request", "pending", {"reason": reason}, datetime.now(UTC).isoformat())
    finally:
        connection.close()


def record_refund_decision(
    *, db_path: str, idempotency_key: str, tenant_id: str, user_id: str,
    order_id: str, reason: str, decision: str, actor_id: str,
    approver_ids: list[str] | None = None,
    message: str = "",
) -> None:
    """CAS transition enforced by the refund service, independently of Graph."""
    from app.config.settings import settings
    authorized_approvers = settings.refund_approver_ids if approver_ids is None else approver_ids
    connection = _connect(db_path)
    error = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        refund_transaction_lock(connection, tenant_id, user_id)
        row = connection.execute(
            "SELECT tenant_id, user_id, status, expires_at, order_id FROM refund_approvals WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if not row or row[:2] != (tenant_id, user_id):
            error = "退款审批请求不存在或身份不符"
        elif row[2] != "pending":
            error = "退款审批已处理，不能重复审批"
        elif order_id != row[4]:
            error = "审批不能更换订单，请重新发起申请"
        elif decision not in {"approve", "edit", "reject"}:
            error = "审批决定无效"
        elif decision in {"approve", "edit"} and actor_id not in authorized_approvers:
            error = "审批人无退款审批权限"
        elif row[3] <= time.time():
            connection.execute("UPDATE refund_approvals SET status = 'expired' WHERE idempotency_key = ?", (idempotency_key,))
            error = "退款审批已超时"
        else:
            connection.execute(
                "UPDATE refund_approvals SET status = ?, actor_id = ?, order_id = ?, reason = ? WHERE idempotency_key = ?",
                (decision, actor_id, order_id, reason, idempotency_key),
            )
        _audit(connection, idempotency_key, tenant_id, user_id, order_id, "approval",
               "denied" if error else decision,
               {"actor_id": actor_id, "reason": reason, "decision_message": message, "error": error}, datetime.now(UTC).isoformat())
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if error:
        raise RuntimeError(error)


def apply_refund(order_id: str, reason: str, **kwargs) -> dict:
    result = _apply_refund(order_id, reason, **kwargs)
    # Include failures before the transaction (missing identity, foreign orders, etc.).
    if result.get("error_code") in {"missing_context", "invalid_reason", "order_not_found", "forbidden"}:
        connection = _connect(kwargs["db_path"])
        try:
            with connection:
                _audit(connection, kwargs.get("idempotency_key", ""), kwargs.get("tenant_id", ""),
                       kwargs.get("user_id", ""), order_id, "execute", "rejected",
                       {**result, "actor_id": kwargs.get("approved_by", "")}, datetime.now(UTC).isoformat())
        finally:
            connection.close()
    return result


def expire_approvals(db_path):
    connection=_connect(db_path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        rows=connection.execute("SELECT idempotency_key,tenant_id,user_id,order_id FROM refund_approvals WHERE status='pending' AND expires_at <= ?", (time.time(),)).fetchall()
        for key,tenant,user,order in rows:
            changed=connection.execute("UPDATE refund_approvals SET status='expired' WHERE idempotency_key=? AND status='pending' AND expires_at <= ?", (key,time.time())).rowcount
            if changed:
                _audit(connection,key,tenant,user,order,'expiry','expired',{'actor_id':'system-timeout'},datetime.now(UTC).isoformat())
        connection.commit()
        return connection.execute("SELECT DISTINCT request_id,tenant_id,user_id FROM refund_approvals WHERE status='expired' AND request_id != ''").fetchall()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
