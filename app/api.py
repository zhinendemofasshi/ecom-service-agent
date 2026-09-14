"""Production-facing FastAPI service for sessions, streaming, and HITL."""

from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterator, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from openai import OpenAI

import psycopg
from app.database import PostgresRunSlots, advisory_lock, DatabaseBusy
from app.session_registry import SessionRegistry
from app.agent.chat import EcomAgent
from app.agent.checkpointing import CheckpointStore
from app.agent.langgraph_runtime import RunCancelled
from app.config.settings import Settings, settings


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class ApprovalDecision(BaseModel):
    call_id: str = Field(min_length=1,max_length=256)
    type: Literal["approve","edit","reject"]
    arguments: dict | None = None
    message: str | None = Field(default=None,max_length=2000)


class ApprovalRequest(BaseModel):
    decisions: list[ApprovalDecision] | None = Field(default=None,min_length=1,max_length=50)
    decision: Literal["approve", "edit", "reject"] | None = None
    arguments: dict | None = None
    message: str | None = Field(default=None, max_length=2_000)


class EventBroker:
    """Thread-safe bounded replay buffer used by the SSE endpoint."""

    def __init__(self, registry: "SessionRegistry", session_id: str, max_events: int = 2_000):
        self.registry = registry
        self.session_id = session_id
        self.max_events = max_events
        self.request_id = ""
        self._condition = threading.Condition()

    def publish(self, event: dict) -> None:
        with self._condition:
            enriched = {
                "timestamp": datetime.now(UTC).isoformat(),
                "request_id": self.request_id,
                **event,
            }
            enriched = self.registry.append_event(
                self.session_id, enriched, self.max_events
            )
            self._condition.notify_all()

    def wait_after(self, sequence: int, timeout: float) -> list[dict]:
        with self._condition:
            found = self.registry.events_after(self.session_id, sequence)
            if found:
                return found
            self._condition.wait(timeout)
            return self.registry.events_after(self.session_id, sequence)


@dataclass
class Identity:
    tenant_id: str
    user_id: str
    actor_id: str
    can_approve: bool
    is_admin: bool


@dataclass
class ManagedSession:
    agent: EcomAgent
    broker: EventBroker
    lock: threading.Lock


class SessionManager:
    def __init__(
        self,
        config: Settings,
        agent_factory: Callable[..., EcomAgent] = EcomAgent,
    ):
        self.config = config
        self.agent_factory = agent_factory
        self.registry = SessionRegistry(config.api_session_db_path, config.state_database_url)
        self.tasks: set[asyncio.Task] = set()
        self.sessions: dict[str, ManagedSession] = {}
        self.lock = threading.Lock()
        self.client = None
        self.checkpoint_store = None
        self._last_cleanup = 0.0
        self.run_slots = (PostgresRunSlots(config.state_database_url, config.max_concurrent_runs)
                          if config.state_database_url else threading.BoundedSemaphore(config.max_concurrent_runs))

    def create(self, identity: Identity) -> str:
        opaque = sha256(f"{identity.tenant_id}:{identity.user_id}:{uuid4()}".encode()).hexdigest()
        session_path = str((Path("app/sessions/api") / f"{opaque}.json").resolve())
        return self.registry.create(identity.tenant_id, identity.user_id, session_path)

    def get(self, session_id: str, identity: Identity) -> ManagedSession:
        try:
            session_path = self.registry.get(session_id, identity.tenant_id, identity.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session 不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="无权访问该 session") from exc
        with self.lock:
            managed = self.sessions.get(session_id)
            if managed is None:
                broker = EventBroker(self.registry, session_id)
                if self.client is None:
                    self.client = OpenAI(
                        api_key=self.config.openai_api_key or "api-not-configured",
                        base_url=self.config.openai_base_url,
                    )
                if self.checkpoint_store is None:
                    self.checkpoint_store = CheckpointStore(
                        backend=self.config.checkpoint_backend,
                        sqlite_path=self.config.langgraph_checkpoint_path,
                        database_url=self.config.database_url,
                    )
                agent = self.agent_factory(
                    config=self.config,
                    session_path=session_path,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    event_sink=broker.publish,
                    client=self.client,
                    checkpoint_store=self.checkpoint_store,
                )
                managed = ManagedSession(agent=agent, broker=broker, lock=threading.Lock())
                self.sessions[session_id] = managed
            return managed

    def expire_pending(self):
        from app.agent.tools.refund import expire_approvals
        for request_id,tenant,user in expire_approvals(self.config.refund_store):
            try:
                row=self.registry.get_run(request_id,tenant,user)
                if row['status']!='pending':
                    continue
                identity=Identity(tenant,user,'system-timeout',False,False)
                managed=self.get(row['session_id'],identity)
                with self.execution(row['session_id'],identity,managed):
                    if self.registry.get_run(request_id,tenant,user)['status']!='pending': continue
                    managed.broker.request_id=request_id
                    managed.agent.expire_pending_approval()
                    self.registry.finish_run(request_id,'expired')
                    managed.broker.publish({'type':'approval_expired','request_id':request_id,'message':'审批超时，未执行退款'})
            except (KeyError,DatabaseBusy):
                continue

    def maintenance(self):
        self.expire_pending()
        if not self.config.state_database_url:
            return
        with self.lock:
            if self.checkpoint_store is None:
                self.checkpoint_store = CheckpointStore(backend=self.config.checkpoint_backend,
                    sqlite_path=self.config.langgraph_checkpoint_path, database_url=self.config.database_url)
            store = self.checkpoint_store
        if time.monotonic()-self._last_cleanup >= self.config.checkpoint_cleanup_interval_seconds:
            store.cleanup(self.config.checkpoint_retention_days)
            from app.agent import document_store
            document_store.cleanup(self.config.state_database_url,self.config.memory_retention_days,self.config.conversation_retention_days)
            self._last_cleanup=time.monotonic()
        self.registry.recover_abandoned(self.config.abandoned_run_seconds)

    @contextmanager
    def execution(self, session_id: str, identity: Identity, managed: ManagedSession):
        guard = (advisory_lock(self.config.state_database_url, "api-user", f"{identity.tenant_id}:{identity.user_id}")
                 if self.config.state_database_url else nullcontext())
        with managed.lock, guard:
            if self.config.state_database_url and hasattr(managed.agent, "reload_state"):
                managed.agent.reload_state()
            yield

    def close(self) -> None:
        with self.lock:
            for managed in self.sessions.values():
                try:
                    if not self.config.state_database_url:
                        managed.agent.save()
                    if managed.agent.graph_runtime is not None:
                        managed.agent.graph_runtime.close()
                    managed.agent.tool_manager.close()
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    pass
            self.sessions.clear()
        self.registry.close()
        if self.client is not None:
            self.client.close()
        if self.checkpoint_store is not None:
            self.checkpoint_store.close()


def create_app(
    config: Settings = settings,
    agent_factory: Callable[..., EcomAgent] = EcomAgent,
) -> FastAPI:
    manager = SessionManager(config, agent_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()
        async def maintain():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(1, min(config.approval_poll_seconds,config.checkpoint_cleanup_interval_seconds)))
                except asyncio.TimeoutError:
                    try:
                        await asyncio.to_thread(manager.maintenance)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception("PostgreSQL maintenance failed")
        maintenance = None
        try:
            if config.state_database_url:
                await asyncio.to_thread(manager.maintenance)
            maintenance = asyncio.create_task(maintain())
            yield
        finally:
            stop.set()
            if maintenance:
                await maintenance
            if manager.tasks:
                await asyncio.gather(*manager.tasks, return_exceptions=True)
            manager.close()
            from app.observability import flush
            await asyncio.to_thread(flush)

    app = FastAPI(title="Ecom Service Agent API", version="1.0.0", lifespan=lifespan)
    app.state.session_manager = manager

    @app.exception_handler(DatabaseBusy)
    async def busy_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def authenticate(
        authorization: str = Header("", alias="Authorization"),
        x_api_key: str = Header("", alias="X-API-Key"),
        x_tenant_id: str = Header("", alias="X-Tenant-ID"),
        x_user_id: str = Header("", alias="X-User-ID"),
        x_actor_id: str = Header("", alias="X-Actor-ID"),
        x_approval_key: str = Header("", alias="X-Approval-Key"),
        x_admin_key: str = Header("", alias="X-Admin-Key"),
    ) -> Identity:
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        principal = next((value for key, value in config.auth_tokens.items()
                          if token and hmac.compare_digest(token, key)), None)
        if principal:
            if not all(principal.get(k) for k in ("tenant_id", "user_id", "actor_id")):
                raise HTTPException(status_code=503, detail="身份配置不完整")
            if principal.get("expires_at", 0) <= time.time():
                raise HTTPException(status_code=401, detail="登录凭证已过期")
            return Identity(principal["tenant_id"], principal["user_id"], principal["actor_id"],
                            "approver" in principal.get("roles", []),
                            "admin" in principal.get("roles", []))
        if not config.trusted_identity_headers:
            raise HTTPException(status_code=401, detail="需要有效的登录 Bearer token")
        if not x_tenant_id or not x_user_id:
            raise HTTPException(status_code=401, detail="身份不能为空")
        if config.service_api_key == "change-me":
            raise HTTPException(status_code=503, detail="SERVICE_API_KEY 尚未安全配置")
        if not hmac.compare_digest(x_api_key, config.service_api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key 无效")
        return Identity(
            x_tenant_id,
            x_user_id,
            x_actor_id or x_user_id,
            bool(x_approval_key) and config.approver_api_key != "change-me-approver" and hmac.compare_digest(x_approval_key, config.approver_api_key),
            bool(x_admin_key) and config.admin_api_key != "change-me-admin" and hmac.compare_digest(x_admin_key, config.admin_api_key),
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict:
        if config.checkpoint_backend == "postgres" and not config.database_url:
            raise HTTPException(status_code=503, detail="PostgreSQL checkpoint 未配置")
        if not config.auth_tokens and not config.trusted_identity_headers:
            raise HTTPException(status_code=503, detail="登录身份未配置")
        if config.trusted_identity_headers and (
            config.service_api_key == "change-me"
            or config.approver_api_key == "change-me-approver"
            or config.admin_api_key == "change-me-admin"
        ):
            raise HTTPException(status_code=503, detail="API 凭证尚未安全配置")
        if not config.openai_api_key:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY 未配置")
        try:
            store = CheckpointStore(
                backend=config.checkpoint_backend,
                sqlite_path=config.langgraph_checkpoint_path,
                database_url=config.database_url,
            )
            store.close()
            with manager.registry.lock:
                manager.registry.connection.execute("SELECT 1").fetchone()
            from app.agent.tools.refund import _connect
            refund_connection = _connect(config.refund_store)
            refund_connection.close()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="数据库依赖不可用") from exc
        return {"status": "ready", "checkpoint_backend": config.checkpoint_backend}

    @app.post("/sessions", status_code=201)
    def create_session(identity: Identity = Depends(authenticate)) -> dict:
        return {"session_id": manager.create(identity)}

    @app.post("/sessions/{session_id}/messages")
    async def send_message(
        session_id: str,
        body: MessageRequest,
        identity: Identity = Depends(authenticate),
        x_request_id: str = Header("", alias="X-Request-ID"),
    ) -> dict:
        managed = manager.get(session_id, identity)
        request_id = x_request_id or str(uuid4())
        if not manager.run_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="并发 Agent run 已达到上限")
        try:
            manager.registry.create_run(
                request_id, session_id, identity.tenant_id, identity.user_id
            )
        except (sqlite3.IntegrityError, psycopg.IntegrityError) as exc:
            with manager.registry.lock:
                manager.registry.connection.rollback()
            manager.run_slots.release()
            raise HTTPException(status_code=409, detail="X-Request-ID 已存在") from exc
        except Exception:
            manager.run_slots.release()
            raise

        def run():
            try:
                with manager.execution(session_id, identity, managed):
                    if manager.registry.get_run(request_id, identity.tenant_id, identity.user_id)["status"] != "running":
                        raise HTTPException(status_code=409, detail="run 已结束")
                    if managed.agent.has_pending_approval:
                        raise HTTPException(status_code=409, detail="请先处理当前待审批操作")
                    managed.broker.request_id = request_id
                    response = managed.agent.chat(
                        body.text, request_id=request_id,
                        cancel_check=lambda: manager.registry.is_cancel_requested(request_id),
                    )
                    pending = managed.agent.has_pending_approval
                    manager.registry.finish_run(request_id, "pending" if pending else "completed")
                    return {"request_id": request_id, "pending_approval": pending,
                            "response": response.model_dump(mode="json")}
            except DatabaseBusy as exc:
                manager.registry.finish_run(request_id, "failed")
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except HTTPException:
                manager.registry.finish_run(request_id, "failed")
                raise
            except RunCancelled as exc:
                manager.registry.finish_run(request_id, "cancelled")
                raise HTTPException(status_code=409, detail="Agent run 已取消") from exc
            except Exception as exc:
                managed.broker.publish({"type": "run_failed", "request_id": request_id, "error": "Agent 执行失败"})
                manager.registry.finish_run(request_id, "failed")
                raise HTTPException(status_code=502, detail="Agent 执行失败") from exc
            finally:
                manager.run_slots.release()

        task = asyncio.create_task(asyncio.to_thread(run))
        manager.tasks.add(task)
        def done(completed):
            manager.tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)

    @app.get("/approvals", include_in_schema=False)
    def approval_page():
        return FileResponse(Path(__file__).parent / 'web' / 'approvals.html', headers={
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})

    @app.get('/sessions/{session_id}/handoffs')
    def handoffs(session_id: str, identity: Identity = Depends(authenticate)):
        manager.get(session_id,identity)
        with manager.registry.lock:
            rows=manager.registry.connection.execute('SELECT request_id,status,payload FROM human_handoffs WHERE session_id=?',(session_id,)).fetchall()
        return {'items':[{'request_id':r[0],'status':r[1],'event':json.loads(r[2])} for r in rows]}

    @app.get('/sessions/{session_id}/approvals')
    def pending_approvals(session_id: str, identity: Identity = Depends(authenticate)):
        managed=manager.get(session_id,identity)
        return {'actions':managed.agent.pending_approvals(),'server_time':time.time()}

    @app.get('/sessions/{session_id}')
    def session_state(session_id: str, identity: Identity = Depends(authenticate)):
        managed=manager.get(session_id,identity)
        with manager.execution(session_id,identity,managed):
            state=managed.agent.graph_runtime.graph.get_state(managed.agent.graph_runtime.config).values
            return {'messages':state.get('conversation',[]),'summary':state.get('summary'),
                    'response':state.get('structured_response'),'message_schema_version':state.get('message_schema_version',1)}

    @app.get("/sessions/{session_id}/events")
    def events(
        session_id: str,
        identity: Identity = Depends(authenticate),
        after: int | None = Query(None, ge=0),
        last_event_id: str = Header("", alias="Last-Event-ID"),
        limit: int = Query(100, ge=1, le=1_000),
        timeout_seconds: float = Query(15.0, ge=0.1, le=60.0),
    ) -> StreamingResponse:
        managed = manager.get(session_id, identity)

        try:
            initial_cursor = after if after is not None else int(last_event_id or "0")
            if initial_cursor < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="无效事件游标") from exc
        with manager.registry.lock:
            oldest, newest = manager.registry.connection.execute(
                "SELECT MIN(sequence), MAX(sequence) FROM api_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if oldest is not None and initial_cursor and initial_cursor < oldest - 1:
            raise HTTPException(status_code=410, detail="事件游标已过期，请重新同步会话")
        if initial_cursor > (newest or 0):
            raise HTTPException(status_code=409, detail="事件游标超出最新事件")

        def generate() -> Iterator[str]:
            cursor = initial_cursor
            emitted = 0
            deadline = time.monotonic() + timeout_seconds
            while emitted < limit and time.monotonic() < deadline:
                batch = managed.broker.wait_after(cursor, min(1.0, max(0.1, deadline - time.monotonic())))
                if not batch:
                    yield ": keepalive\n\n"
                    continue
                for event in batch:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    emitted += 1
                    if emitted >= limit:
                        break

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/sessions/{session_id}/approval")
    async def approval(
        session_id: str,
        body: ApprovalRequest,
        identity: Identity = Depends(authenticate),
        expected_request_id: str | None = None,
    ) -> dict:
        if bool(body.decisions) == bool(body.decision):
            raise HTTPException(status_code=422,detail="请提供 decision 或 decisions 其中之一")
        approving=body.decision in {"approve","edit"} or any(d.type in {"approve","edit"} for d in body.decisions or [])
        if approving and (not identity.can_approve or identity.actor_id not in config.refund_approver_ids):
            raise HTTPException(status_code=403, detail="审批凭证无效")
        if body.decision == "edit" and body.arguments is None:
            raise HTTPException(status_code=422, detail="edit 必须提供 arguments")
        managed = manager.get(session_id, identity)

        if not manager.run_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="并发 Agent run 已达到上限")

        def run():
            try:
                with manager.execution(session_id, identity, managed):
                    with manager.registry.lock:
                        pending = manager.registry.connection.execute(
                            "SELECT request_id FROM api_runs WHERE session_id = ? AND status = 'pending'",
                            (session_id,),
                        ).fetchone()
                    if not pending or (expected_request_id and pending[0] != expected_request_id):
                        raise HTTPException(status_code=409, detail="run 不处于待审批状态")
                    managed.broker.request_id = pending[0]
                    try:
                        if body.decisions:
                            response=managed.agent.resume_decisions([d.model_dump(exclude_none=True) for d in body.decisions],identity.actor_id)
                        else:
                            response = managed.agent.resume_approval(
                                body.decision != "reject",
                                arguments=body.arguments if body.decision == "edit" else None,
                                message=body.message, approved_by=identity.actor_id,
                            )
                    except RuntimeError as exc:
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                    except Exception as exc:
                        manager.registry.finish_run(pending[0], "failed")
                        managed.broker.publish({"type": "run_failed", "error": "审批后执行失败"})
                        raise HTTPException(status_code=502, detail="审批后执行失败") from exc
                    still_pending = managed.agent.has_pending_approval
                    manager.registry.finish_run(pending[0], "pending" if still_pending else "completed")
                    return {"pending_approval": still_pending, "response": response.model_dump(mode="json")}
            finally:
                manager.run_slots.release()

        task = asyncio.create_task(asyncio.to_thread(run))
        manager.tasks.add(task)
        def done(completed):
            manager.tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)

    def owned_run(request_id: str, identity: Identity) -> dict:
        try:
            return manager.registry.get_run(request_id, identity.tenant_id, identity.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run 不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="无权访问该 run") from exc

    @app.post("/runs/{request_id}/approve")
    async def approve_run(request_id: str, identity: Identity = Depends(authenticate)) -> dict:
        row = owned_run(request_id, identity)
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="run 不处于待审批状态")
        return await approval(row["session_id"], ApprovalRequest(decision="approve"), identity, request_id)

    @app.post("/runs/{request_id}/reject")
    async def reject_run(request_id: str, body: ApprovalRequest | None = None, identity: Identity = Depends(authenticate)) -> dict:
        row = owned_run(request_id, identity)
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="run 不处于待审批状态")
        return await approval(row["session_id"], ApprovalRequest(decision="reject",message=body.message if body else None), identity, request_id)

    @app.post("/runs/{request_id}/cancel", status_code=202)
    async def cancel_run(request_id: str, identity: Identity = Depends(authenticate)) -> dict:
        row = owned_run(request_id, identity)
        if row["status"] == "pending":
            managed = manager.get(row["session_id"], identity)
            def cancel_pending():
                with manager.execution(row["session_id"], identity, managed):
                    if owned_run(request_id, identity)["status"] != "pending":
                        raise HTTPException(status_code=409, detail="run 已处理")
                    managed.broker.request_id = request_id
                    managed.agent.cancel_pending_approval(identity.actor_id)
                    manager.registry.finish_run(request_id, "cancelled")
            await asyncio.to_thread(cancel_pending)
            return {"request_id": request_id, "status": "cancelled"}
        try:
            run_status = manager.registry.cancel_run(
                request_id, identity.tenant_id, identity.user_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run 不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="无权取消该 run") from exc
        return {"request_id": request_id, "status": run_status}

    @app.get("/runs/{request_id}")
    def get_run(request_id: str, identity: Identity = Depends(authenticate)) -> dict:
        try:
            return manager.registry.get_run(
                request_id, identity.tenant_id, identity.user_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run 不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="无权访问该 run") from exc

    @app.post("/admin/checkpoints/cleanup")
    def cleanup(identity: Identity = Depends(authenticate)) -> dict:
        if not identity.is_admin:
            raise HTTPException(status_code=403, detail="管理凭证无效")
        store = manager.checkpoint_store or CheckpointStore(
            backend=config.checkpoint_backend,
            sqlite_path=config.langgraph_checkpoint_path,
            database_url=config.database_url,
        )
        owns_store = manager.checkpoint_store is None
        try:
            deleted = store.cleanup(config.checkpoint_retention_days)
        finally:
            if owns_store:
                store.close()
        return {"deleted_threads": deleted}

    return app


app = create_app()
