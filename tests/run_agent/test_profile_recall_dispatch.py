"""Exercise both agent dispatch paths against disjoint real profile databases."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tool_executor import execute_tool_calls_sequential
from hermes_state import SessionDB
from run_agent import AIAgent


@pytest.fixture
def profile_stores(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    stores = {}
    for name, folder in (("default", home), ("engineer", home / "profiles" / "engineer")):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.yaml").write_text("{}\n")
        db = SessionDB(folder / "state.db")
        db.create_session(name + "-only", source="cli")
        db.append_message(name + "-only", role="user", content="sharedneedle " + name)
        stores[name] = db
    yield stores
    for db in stores.values():
        db.close()


def make_agent(db):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            session_db=db, session_id="active-session", platform="telegram",
        )


def dispatch(agent, route, args):
    if route == "invoke":
        return json.loads(agent._invoke_tool("session_search", args, "recall-test"))
    messages = []
    message = SimpleNamespace(tool_calls=[SimpleNamespace(
        id="recall-call", function=SimpleNamespace(
            name="session_search", arguments=json.dumps(args),
        ),
    )])
    execute_tool_calls_sequential(agent, message, messages, effective_task_id="recall-test")
    return json.loads(next(m["content"] for m in messages if m["role"] == "tool"))


@pytest.mark.parametrize("route", ["invoke", "sequential"])
@pytest.mark.parametrize("query", ["sharedneedle", ""])
def test_explicit_profile_is_respected(route, query, profile_stores):
    agent = make_agent(profile_stores["default"])
    result = dispatch(agent, route, {"query": query, "profile": "engineer"})
    assert result["success"] is True
    rows = result.get("results", result.get("sessions", []))
    assert [r["session_id"] for r in rows] == ["engineer-only"]
    # A topic role overlay must not move the agent's current/default store.
    assert agent._session_db is profile_stores["default"]
    result = dispatch(agent, route, {"query": query})
    rows = result.get("results", result.get("sessions", []))
    assert [r["session_id"] for r in rows] == ["default-only"]


@pytest.mark.parametrize("route", ["invoke", "sequential"])
def test_nonexistent_profile_does_not_fall_back(route, profile_stores):
    result = dispatch(make_agent(profile_stores["default"]), route,
                      {"query": "sharedneedle", "profile": "missing"})
    assert result["success"] is False
    assert "does not exist" in result["error"]
