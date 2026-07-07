"""Regression coverage for clarify context through agent-runtime dispatch."""

import json
from types import SimpleNamespace


def test_invoke_tool_passes_clarify_context_to_callback():
    """The concurrent/runtime path must not drop the schema-level context field."""
    from agent.agent_runtime_helpers import invoke_tool

    seen = {}

    def clarify_callback(question, choices):
        seen["question"] = question
        seen["choices"] = choices
        return "Use the fix"

    agent = SimpleNamespace(
        session_id="session-1",
        _current_turn_id="turn-1",
        _current_api_request_id="api-1",
        _memory_manager=None,
        clarify_callback=clarify_callback,
    )

    result = json.loads(invoke_tool(
        agent,
        "clarify",
        {
            "context": "I found the bug and tests passed.",
            "question": "Which next action should I take?",
            "choices": ["Use the fix", "Leave it"],
        },
        effective_task_id="task-1",
        tool_call_id="call-1",
        pre_tool_block_checked=True,
    ))

    assert seen["question"] == (
        "I found the bug and tests passed.\n\n"
        "Which next action should I take?"
    )
    assert seen["choices"] == ["Use the fix", "Leave it"]
    assert result["context"] == "I found the bug and tests passed."
    assert result["user_response"] == "Use the fix"
