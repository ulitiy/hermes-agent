"""ACP approval choices must not broaden consent or prompt during discovery."""

import io
import json
from types import SimpleNamespace

import pytest

from agent.copilot_acp_client import CopilotACPClient


@pytest.mark.parametrize("choice,options,expected", [
    ("cancel", [{"optionId": "allow_once", "kind": "allow_once"}], "cancelled"),
    ("once", [{"optionId": "allow_always", "kind": "allow_always"}], "cancelled"),
    ("once", [{"optionId": "allow_once", "kind": "allow_once"}], "selected"),
])
def test_permission_scope_is_not_broadened(monkeypatch, choice, options, expected):
    monkeypatch.setattr("agent.copilot_acp_client.request_acp_permission_approval", lambda **kwargs: choice)
    process = SimpleNamespace(stdin=io.StringIO())
    CopilotACPClient(acp_cwd="/tmp")._handle_server_message(
        {"id": 1, "method": "session/request_permission", "params": {"options": options}},
        process=process, cwd="/tmp", text_parts=[], reasoning_parts=[],
    )
    outcome = json.loads(process.stdin.getvalue())["result"]["outcome"]
    assert outcome["outcome"] == expected
    if expected == "selected":
        assert outcome["optionId"] == "allow_once"


def test_model_discovery_never_prompts_for_permission(monkeypatch):
    def unexpected_prompt(**kwargs):
        pytest.fail("Model discovery must not request action approval")

    monkeypatch.setattr("agent.copilot_acp_client.request_acp_permission_approval", unexpected_prompt)
    process = SimpleNamespace(stdin=io.StringIO())
    CopilotACPClient(acp_cwd="/tmp")._handle_server_message(
        {"id": 1, "method": "session/request_permission", "params": {
            "options": [{"optionId": "allow_once", "kind": "allow_once"}],
        }},
        process=process, cwd="/tmp", text_parts=[], reasoning_parts=[], allow_file_requests=False,
    )
    assert json.loads(process.stdin.getvalue())["result"]["outcome"]["outcome"] == "cancelled"
