"""Local approval contracts moved off the deprecated, upstream-replaced ACP test suite."""

import io
import json
from types import SimpleNamespace

import pytest

from agent.acp_client import ACPClient
from agent.copilot_acp_client import CopilotACPClient


def dispatch(client, method, params, cwd):
    process = SimpleNamespace(stdin=io.StringIO())
    assert client._handle_server_message(
        {"jsonrpc": "2.0", "id": 11, "method": method, "params": params},
        process=process, cwd=str(cwd), text_parts=[], reasoning_parts=[],
    )
    return json.loads(process.stdin.getvalue())


@pytest.mark.parametrize("choice,expected", [
    ("once", "allow_once"), ("deny", "reject_once"), ("always", "accept_execpolicy_amendment"),
])
def test_permission_bridge_selects_exact_scope(monkeypatch, tmp_path, choice, expected):
    calls = []

    def approve(**kwargs):
        calls.append(kwargs)
        return choice

    monkeypatch.setattr("agent.acp_permissions.request_acp_permission_approval", approve)
    response = dispatch(CopilotACPClient(acp_cwd=str(tmp_path)), "session/request_permission", {
        "toolCall": {"kind": "execute", "title": "docker compose ps",
                     "rawInput": {"command": "docker compose ps", "cwd": "/repo"}},
        "options": [
            {"optionId": "allow_once", "kind": "allow_once"},
            {"optionId": "allow_always", "kind": "allow_always"},
            {"optionId": "accept_execpolicy_amendment", "kind": "allow_always"},
            {"optionId": "reject_once", "kind": "reject_once"},
        ],
    }, tmp_path)
    assert response["result"]["outcome"] == {"outcome": "selected", "optionId": expected}
    assert len(calls) == 1
    assert "docker compose ps" in calls[0]["command"] and "/repo" in calls[0]["command"]
    assert "Codex ACP" in calls[0]["description"]


def test_internal_hub_file_is_blocked(monkeypatch, tmp_path):
    home = tmp_path / "home"
    blocked = home / ".hermes" / "skills" / ".hub" / "index-cache" / "entry.json"
    blocked.parent.mkdir(parents=True)
    blocked.write_text('{"data":"private-hub-state"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    response = dispatch(ACPClient(acp_cwd=str(home)), "fs/read_text_file", {"path": str(blocked)}, home)
    assert "error" in response
