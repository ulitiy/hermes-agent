"""Failures stay observable without exposing remote payloads or credentials."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_legacy_bearer_is_a_rotatable_secret(monkeypatch):
    from hermes_a2a_sidecar.auth import authenticate_peer
    from hermes_a2a_sidecar.config import SidecarConfig
    from starlette.requests import Request

    monkeypatch.setenv("HERMES_A2A_TOKEN", "old-test-token")
    config = SidecarConfig.from_mapping({})
    def request(token):
        return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
    assert authenticate_peer(request("old-test-token"), config).peer_id == "env"
    assert authenticate_peer(request("wrong-token"), config) is None
    monkeypatch.setenv("HERMES_A2A_TOKEN", "new-test-token")
    assert authenticate_peer(request("old-test-token"), config) is None
    assert authenticate_peer(request("new-test-token"), config).peer_id == "env"


def test_malformed_artifact_metadata_warns_without_payload(caplog):
    from hermes_a2a_sidecar.artifacts import _metadata_dict
    assert _metadata_dict({"secret": "do-not-log-this-payload"}) == {}
    assert "metadata" in caplog.text.lower()
    assert "do-not-log-this-payload" not in caplog.text


@pytest.mark.asyncio
async def test_cancel_failure_is_observable(monkeypatch, caplog):
    import hermes_a2a_sidecar.executor as module
    from a2a.types import Task
    from hermes_a2a_sidecar.config import SidecarConfig

    task = Task(id="a2a-fixture", context_id="fixture")
    task.metadata.update({"hermes_kanban_id": "t_fixture"})
    context = SimpleNamespace(task_id=task.id, context_id=task.context_id, current_task=task)
    updater = SimpleNamespace(cancel=AsyncMock(), task_id=task.id, context_id=task.context_id)
    monkeypatch.setattr(module, "TaskUpdater", lambda *a, **k: updater)
    private_payload = "do-not-log-this-payload"
    def broken(**kw):
        raise OSError(private_payload)
    monkeypatch.setattr(module, "connect_closing", broken)
    await module.HermesKanbanExecutor(SidecarConfig(), None).cancel(context, None)
    updater.cancel.assert_awaited_once()
    assert "cancel" in caplog.text.lower()
    assert "do-not-log-this-payload" not in caplog.text
