"""Exercise both real agent dispatch paths with disjoint temporary profile stores."""
import json

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tool_executor import execute_tool_calls_sequential
from hermes_state import SessionDB
from run_agent import AIAgent


@pytest.fixture
def profile_stores(tmp_path, monkeypatch):
    from hermes_cli import profiles

    home = tmp_path / "profile-root"
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
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
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
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
    content = next(m["content"] for m in messages if m["role"] == "tool")
    # A -> B -> A deliberately repeats a read; keep the native loop detector
    # enabled and accept only its expected advisory after the JSON payload.
    result, end = json.JSONDecoder().raw_decode(content)
    suffix = content[end:].strip()
    assert not suffix or suffix.startswith("[Tool loop warning:")
    return result


@pytest.mark.parametrize("route", ["invoke", "sequential"])
@pytest.mark.parametrize("query", ["sharedneedle", ""])
def test_explicit_profile_is_respected(route, query, profile_stores):
    agent = make_agent(profile_stores["default"])
    # A -> B -> A: the explicit cross-profile read must not move the topic store.
    for profile, expected in ((None, "default-only"), ("engineer", "engineer-only"), (None, "default-only")):
        args = {"query": query}
        if profile:
            args["profile"] = profile
        result = dispatch(agent, route, args)
        assert result["success"] is True
        rows = result.get("results", result.get("sessions", []))
        assert [r["session_id"] for r in rows] == [expected]
        assert agent._session_db is profile_stores["default"]


@pytest.mark.parametrize("route", ["invoke", "sequential"])
def test_nonexistent_profile_does_not_fall_back(route, profile_stores):
    result = dispatch(make_agent(profile_stores["default"]), route,
                      {"query": "sharedneedle", "profile": "missing"})
    assert result["success"] is False
    assert "does not exist" in result["error"]
