"""Delegation must not confer the dispatcher's task ownership.

Adapted from trkim's upstream 47bbc12e18a6eeb6196a9ec28391fb13709e70fd.
Use a real board and the actual child runner; only the model is replaced.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    conn = kb.connect()
    tid = kb.create_task(conn, title="parent", assignee="test-worker",
                         workspace_kind="dir", workspace_path=str(workspace))
    run = kb.claim_task(conn, tid, claimer="test-owner")
    assert run is not None
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "test-owner")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    return kb, tid, workspace


class Child:
    tool_progress_callback = None
    _credential_pool = None
    model = "test-model"

    def __init__(self, action):
        self.action = action

    def run_conversation(self, **kwargs):
        self.observed = self.action()
        return {"final_response": json.dumps(self.observed), "completed": True,
                "api_calls": 0, "messages": []}

    def close(self):
        pass


@pytest.mark.parametrize("operation", ["block", "complete"])
def test_real_child_runner_cannot_close_parent(worker, operation):
    from agent.kanban_stop import build_kanban_stop_nudge
    from tools import delegate_tool, kanban_tools as kt

    kb, tid, workspace = worker

    def action():
        return {"nudge": build_kanban_stop_nudge(messages=[]),
                "result": json.loads(getattr(kt, f"_handle_{operation}")(
                    {"task_id": tid, "summary": "child result", "reason": "child blocked"}))}

    child = Child(action)
    result = delegate_tool._run_single_child(0, "review", child, SimpleNamespace())
    assert result["status"] == "completed", result
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
        assert kb.latest_run(conn, tid).status == "running"
    finally:
        conn.close()
    assert child.observed["nudge"] is None
    assert "error" in child.observed["result"]
    assert workspace.is_dir()


def test_two_children_leave_parent_authority_and_heartbeat_intact(worker, monkeypatch):
    from agent.kanban_stop import build_kanban_stop_nudge
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools import delegate_tool, kanban_tools as kt
    from tools.registry import invalidate_check_fn_cache
    from tools.thread_context import propagate_context_to_thread

    kb, tid, _ = worker
    barrier = Barrier(3)
    original = {k: v for k, v in os.environ.items() if k.startswith("HERMES_KANBAN_")}
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    monkeypatch.setattr(kt, "_auto_heartbeat_last_attempt", 0)

    def names():
        return {s["function"]["name"] for s in get_tool_definitions(
            enabled_toolsets=["kanban"], quiet_mode=True)}

    # Prime both caches with the owner: child schema must not reuse this entry.
    assert "kanban_complete" in names()

    def action():
        barrier.wait(timeout=10)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(propagate_context_to_thread(
                lambda: json.loads(kt._handle_block({"reason": "not mine"})))).result(timeout=10)
        observed = {"result": result, "nudge": build_kanban_stop_nudge(messages=[]),
                    "names": sorted(names()), "heartbeat": kt.heartbeat_current_worker_from_env()}
        barrier.wait(timeout=10)
        return observed

    children = [Child(action), Child(action)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(delegate_tool._run_single_child, i, "review", child,
                               SimpleNamespace()) for i, child in enumerate(children)]
        barrier.wait(timeout=10)
        try:
            assert build_kanban_stop_nudge(messages=[])
            assert "kanban_complete" in names()
            assert kt.heartbeat_current_worker_from_env()
        finally:
            barrier.wait(timeout=10)
        results = [f.result(timeout=10) for f in futures]
    assert all(r["status"] == "completed" for r in results), results
    for child in children:
        assert "error" in child.observed["result"]
        assert child.observed["nudge"] is None
        assert not child.observed["names"]
        assert child.observed["heartbeat"] is False
    assert original == {k: v for k, v in os.environ.items() if k.startswith("HERMES_KANBAN_")}
    assert "kanban_complete" in names()
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
        assert kb.get_task(conn, tid).last_heartbeat_at
    finally:
        conn.close()


@pytest.mark.parametrize("surface", ["cli", "db"])
def test_child_terminal_descendant_cannot_mutate_parent(worker, monkeypatch, surface):
    from agent.delegation_context import delegated_child_context
    from tools.environments.local import LocalEnvironment

    kb, tid, workspace = worker
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    if surface == "cli":
        code = (
            "import argparse; from hermes_cli import kanban; "
            "p=argparse.ArgumentParser(); kanban.build_parser(p.add_subparsers()); "
            f"a=p.parse_args(['kanban','block',{tid!r},'child terminal']); "
            "raise SystemExit(kanban.kanban_command(a))"
        )
    else:
        code = (
            "from hermes_cli import kanban_db as kb; c=kb.connect(); "
            f"kb.block_task(c, {tid!r}, reason='child db')"
        )
    with delegated_child_context():
        env = LocalEnvironment(cwd=str(workspace), timeout=15)
        try:
            result = env.execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", timeout=15)
        finally:
            env.cleanup()
    assert result["returncode"] != 0, result
    assert "delegate_task child" in result["output"], result
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


@pytest.mark.parametrize("role", ["leaf", "orchestrator"])
def test_child_construction_has_no_worker_prompt_or_schema(worker, monkeypatch, role):
    from agent.delegation_context import owned_kanban_task
    from agent.prompt_builder import KANBAN_GUIDANCE
    from tools import delegate_tool

    _, tid, _ = worker
    parent = SimpleNamespace(model="gpt-4o-mini", provider="openai",
                             base_url="http://127.0.0.1:1/v1", api_key="test",
                             enabled_toolsets=["kanban", "hermes-cli"])
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 3)
    monkeypatch.setattr(delegate_tool, "_get_orchestrator_enabled", lambda: True)
    child = delegate_tool._build_child_agent(0, "review", None, ["kanban"], None, 2, 1,
                                             parent, role=role)
    try:
        assert child._delegate_role == role
        assert child._kanban_worker_guidance == ""
        assert not any(n.startswith("kanban_") for n in child.valid_tool_names)
        # Exercise real prompt assembly outside construction: a cached child
        # must not acquire its parent's lifecycle guidance on a later turn.
        from agent.system_prompt import build_system_prompt_parts
        assert KANBAN_GUIDANCE not in build_system_prompt_parts(child)["stable"]
        assert owned_kanban_task() == tid
    finally:
        child.close()


@pytest.mark.parametrize("name,args", [
    ("complete", {"summary": "not mine"}),
    ("block", {"reason": "not mine"}),
    ("heartbeat", {}),
    ("comment", {"body": "not mine"}),
    ("create", {"title": "not mine", "assignee": "worker"}),
    ("link", {}),
    ("unblock", {}),
])
def test_all_direct_mutators_refuse_child(worker, name, args):
    from agent.delegation_context import delegated_child_context
    from tools import kanban_tools as kt

    kb, tid, _ = worker
    with kb.connect_closing() as conn:
        before = [ev.id for ev in kb.list_events(conn, tid)]
    with delegated_child_context():
        result = json.loads(getattr(kt, "_handle_" + name)({"task_id": tid, **args}))
    assert "delegate_task child" in result["error"]
    with kb.connect_closing() as conn:
        assert [ev.id for ev in kb.list_events(conn, tid)] == before


def test_codex_spawn_overlay_cannot_restore_parent_identity(worker, monkeypatch):
    from io import BytesIO
    from unittest.mock import Mock
    from agent.delegation_context import delegated_child_context, KANBAN_ENV_KEYS
    from agent.transports import codex_app_server as cas

    _, tid, _ = worker
    captured = []

    def popen(command, **kwargs):
        captured.append((command, kwargs["env"]))
        return Mock(stdin=BytesIO(), stdout=BytesIO(), stderr=BytesIO())

    monkeypatch.setattr(cas.subprocess, "Popen", popen)
    with delegated_child_context():
        client = cas.CodexAppServerClient(env={"HERMES_KANBAN_TASK": tid})
    try:
        command, env = captured[0]
        assert all(key not in env for key in KANBAN_ENV_KEYS)
        assert env["HERMES_DELEGATED_CHILD_CONTEXT"]
        assert not any("writable_roots" in arg for arg in command)
    finally:
        client.close()
