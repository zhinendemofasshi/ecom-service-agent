"""Offline by default. External suites require an explicit --suite selection."""
import os
import socket
import tempfile
from pathlib import Path

import pytest

# Do not read developer credentials or reuse their state in offline tests.
def pytest_addoption(parser):
    parser.addoption("--suite", choices=("unit", "integration", "e2e", "eval"), default="unit")


def pytest_configure(config):
    if config.getoption("--suite") == "unit":
        os.environ["ECOM_ENV_FILE"] = ""
        for key in list(os.environ):
            if key.startswith(("OPENAI_", "LANGSMITH_", "LANGCHAIN_")):
                os.environ.pop(key)
        from app.config.settings import settings
        for field in type(settings).model_fields:
            os.environ.pop(field.upper(), None)
        config._ecom_test_state = tempfile.TemporaryDirectory(prefix="ecom-unit-")
        root = Path(config._ecom_test_state.name)
        for field, leaf in {
            "api_session_db_path": "api.sqlite", "refund_db_path": "refund.sqlite",
            "langgraph_checkpoint_path": "checkpoint.sqlite", "session_path": "session.json",
            "memory_dir": "memory", "kb_index_path": "kb.json", "chroma_persist_dir": "chroma",
        }.items():
            os.environ[field.upper()] = str(root / leaf)
        clean = type(settings)(_env_file=None, openai_api_key="offline-test-key")
        for field in type(settings).model_fields:
            setattr(settings, field, getattr(clean, field))


def pytest_collection_modifyitems(config, items):
    selected, deselected = [], []
    suite = config.getoption("--suite")
    for item in items:
        groups = {m.name for m in item.iter_markers()} & {"unit", "integration", "e2e", "eval"}
        if not groups:
            item.add_marker(pytest.mark.unit)
            groups = {"unit"}
        (selected if suite in groups else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    if request.config.getoption("--suite") != "unit":
        return
    def denied(*args, **kwargs):
        raise AssertionError("Network disabled in unit tests; use a fake client or --suite.")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def pytest_unconfigure(config):
    temporary = getattr(config, "_ecom_test_state", None)
    if temporary is not None:
        temporary.cleanup()
