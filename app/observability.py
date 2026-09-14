"""Explicit, content-free LangSmith tracing; telemetry must never fail a run."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
from hashlib import sha256
import logging
import time

_parent = ContextVar("ecom_trace_parent", default=None)
_config = ContextVar("ecom_trace_config", default=None)
log = logging.getLogger(__name__)
_clients = []


def identifier(value):
    """Stable opaque correlation; never upload caller-controlled identifier text."""
    return sha256(str(value).encode()).hexdigest() if value else ""


@lru_cache(maxsize=8)
def _client(endpoint, key):
    from langsmith import Client
    client = Client(api_url=endpoint, api_key=key, timeout_ms=1000,
                    hide_inputs=True, hide_outputs=False, omit_traced_runtime_info=True)
    _clients.append(client)
    return client


class Span:
    def __init__(self):
        self.outputs = {}
        self.metadata = {}
        self.error = None


@contextmanager
def span(name, kind="chain", *, config=None, metadata=None):
    cfg = config or _config.get()
    if cfg is None:
        from app.config.settings import settings
        cfg = settings
    state = Span()
    run = None
    parent_token = None
    config_token = _config.set(cfg)
    started = time.perf_counter()
    try:
        if cfg.langsmith_tracing and cfg.langsmith_api_key:
            try:
                from langsmith.run_trees import RunTree
                parent = _parent.get()
                fields = dict(name=name, run_type=kind, inputs={},
                              extra={"metadata": metadata or {}})
                if parent is not None:
                    run = parent.create_child(**fields)
                else:
                    run = RunTree(**fields, project_name=cfg.langsmith_project,
                                  client=_client(cfg.langsmith_endpoint, cfg.langsmith_api_key))
                run.post()
                parent_token = _parent.set(run)
            except Exception:
                log.warning("Trace export unavailable")
                run = None
        try:
            yield state
        except BaseException as exc:
            # Graph interrupt is a normal durable pause, not a model/tool error.
            if type(exc).__name__ != "GraphInterrupt":
                state.error = type(exc).__name__
            raise
    finally:
        if run is not None:
            try:
                run.end(outputs=state.outputs, error=state.error,
                        metadata={**state.metadata, "duration_ms": (time.perf_counter()-started)*1000})
                run.patch()
            except Exception:
                log.warning("Trace export unavailable")
        if parent_token is not None:
            _parent.reset(parent_token)
        _config.reset(config_token)


def node_span(name, operation):
    @wraps(operation)
    def traced(*args, **kwargs):
        with span(name):
            return operation(*args, **kwargs)
    return traced


@contextmanager
def graph_tracing_disabled():
    # Auto LangGraph tracing serializes complete checkpoint state. Use only our
    # explicit spans until a content-aware privacy policy is available.
    from langsmith import tracing_context
    with tracing_context(enabled=False):
        yield


def record_response(trace, response):
    trace.metadata["response_id"] = identifier(getattr(response, "id", ""))
    usage = getattr(response, "usage", None)
    if usage is not None:
        data = {key: getattr(usage, source, 0) for key, source in (
            ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"))}
        details = getattr(usage, "input_tokens_details", None)
        if details is not None:
            data["input_token_details"] = {"cache_read": getattr(details, "cached_tokens", 0)}
        trace.outputs["usage_metadata"] = data
    if getattr(response, "status", None) in {"failed", "incomplete", "cancelled"}:
        raise RuntimeError("模型未返回完整结果")


def flush():
    """Bounded best-effort drain after business tasks finish."""
    deadline = time.monotonic() + 2
    for client in list(_clients):
        try:
            client.flush(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            log.warning("Trace flush unavailable")


def current_trace_ids():
    run = _parent.get()
    return {"trace_id": str(run.trace_id), "span_id": str(run.id)} if run else {}
