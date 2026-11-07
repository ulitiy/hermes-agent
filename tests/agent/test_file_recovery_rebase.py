"""File-recovery continuations must not become user history after semantic rebase."""

from copy import deepcopy
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_state import SessionDB
from run_agent import AIAgent


@pytest.fixture
def recovery_agent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    # No background auxiliary-model work or callbacks after the temporary DB closes.
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *args, **kwargs: None)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "display": {"file_mutation_auto_recovery": True},
        "model": {"context_length": 128000},
    }))
    db = SessionDB(home / "state.db")
    sid = "file-recovery-test"
    db.create_session(sid, source="cli")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            session_id=sid, session_db=db, api_key="test-key",
            base_url="https://example.invalid/v1", provider="openai-compat",
            model="test/model", max_iterations=4, quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
    agent._session_db_created = True
    agent._cached_system_prompt = "stable test prompt"
    agent.save_trajectories = False
    agent.compression_enabled = False
    try:
        yield agent, db
    finally:
        db.close()


def _response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=None), finish_reason="stop",
        )], model="test/model", usage=None,
    )


@pytest.mark.parametrize("repair", [True, False])
def test_recovery_is_once_per_turn_and_never_durable(recovery_agent, tmp_path, repair):
    from agent.conversation_compression import _is_real_user_message

    agent, db = recovery_agent
    target = tmp_path / "document.md"
    calls = []
    emitted = []
    agent.interim_assistant_callback = lambda text, **kwargs: emitted.append(text)
    candidate = "Premature claim: the edit is complete"
    final = "Verified repaired file" if repair else "Cannot edit; approval is needed"

    def model_call(kwargs):
        step = len(calls) % 2
        calls.append(kwargs)
        if step == 0:
            target.write_text("original")
            agent._record_file_mutation_result(
                "patch", {"mode": "replace", "path": str(target)},
                '{"error":"old text not found"}', is_error=True,
            )
            return _response(candidate)
        scaffolding = [m for m in agent._session_messages if m.get("_file_mutation_recovery_synthetic")]
        assert len(scaffolding) == 2
        assert not _is_real_user_message(scaffolding[-1])
        # Force the real intermediate flush; a crash here must not replay the retry prompt.
        agent._flush_messages_to_session_db(agent._session_messages, None)
        assert candidate not in str(db.get_messages_as_conversation(agent.session_id))
        if repair:
            target.write_text("repaired")
        return _response(final)

    agent._interruptible_api_call = model_call
    history = None
    for prompt in ("Edit the document", "Edit it again"):
        result = agent.run_conversation(prompt, conversation_history=history)
        assert result["final_response"].startswith(final)
        assert result["completed"] is True
        assert not any(m.get("_file_mutation_recovery_synthetic") for m in result["messages"])
        assert candidate not in str(db.get_messages_as_conversation(agent.session_id))
        assert candidate not in emitted
        assert agent._cached_system_prompt == "stable test prompt"
        history = result["messages"]
    assert len(calls) == 4  # one retry, reset for the second turn, even if repair fails


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_budget_limited_recovery_never_reuses_rejected_answer(recovery_agent, tmp_path, enabled):
    from hermes_constants import get_hermes_home

    agent, _db = recovery_agent
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({
        "display": {"file_mutation_auto_recovery": enabled},
    }))
    agent.max_iterations = 1
    agent._handle_max_iterations = MagicMock(return_value="Budget exhausted; file not edited")
    calls = []

    def model_call(kwargs):
        calls.append(kwargs)
        # Isolate the API-call cap from the independently refundable budget
        # to exercise the continuation handoff into the budget finalizer.
        agent.iteration_budget.max_total = 2
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": str(tmp_path / "missing.md")},
            '{"error":"missing file"}', is_error=True,
        )
        return _response("Cannot edit the missing file")

    agent._interruptible_api_call = model_call
    result = agent.run_conversation("Edit the missing file")
    expected = "Budget exhausted; file not edited" if enabled else "Cannot edit the missing file"
    assert result["final_response"].startswith(expected)
    assert agent._handle_max_iterations.call_count == int(enabled)
    assert len(calls) == 1


@pytest.mark.parametrize("repair", [True, False])
def test_failed_edit_precedes_late_lsp_without_losing_durable_verdict(recovery_agent, tmp_path, repair):
    from agent.lsp import deferred

    agent, db = recovery_agent
    target = tmp_path / "document.md"
    candidate = "Premature claim: both edits are complete"
    diagnostic = "other.py:1: new undefined name from successful edit"
    final = "Edits verified" if repair else "The document edit still failed"
    emitted, calls = [], []
    agent.interim_assistant_callback = lambda text, **kwargs: emitted.append(text)

    def model_call(kwargs):
        step = len(calls) % 2
        calls.append(deepcopy(kwargs))
        if step == 0:
            target.write_text("original")
            agent._record_file_mutation_result(
                "patch", {"mode": "replace", "path": str(target)},
                '{"error":"old text not found"}', is_error=True,
            )
            deferred.post(agent.session_id, str(tmp_path / "other.py"), diagnostic, "original")
            return _response(candidate)
        # The simultaneous diagnostic is durable BEFORE another model/tool boundary.
        durable = db.get_messages_as_conversation(agent.session_id)
        assert diagnostic in str(durable)
        assert candidate not in str(durable)
        assert candidate not in str(kwargs["messages"])
        assert candidate not in emitted
        assert agent._file_mutation_recovery_attempts == 1
        scaffolding = [m for m in agent._session_messages if m.get("_file_mutation_recovery_synthetic")]
        assert len(scaffolding) == 2
        assert all(m["content"] not in [row.get("content") for row in durable] for m in scaffolding)
        assert diagnostic in str(kwargs["messages"])
        assert agent._lsp_turn_end_notes == 1
        prefix = calls[-2]["messages"]
        assert kwargs["messages"][:len(prefix)] == prefix
        roles = [m["role"] for m in kwargs["messages"]]
        assert all(a != b for a, b in pairwise(roles))
        if repair:
            target.write_text("repaired")
        return _response(final)

    agent._interruptible_api_call = model_call
    history = None
    for prompt in ("Edit both files", "Edit both files again"):
        result = agent.run_conversation(prompt, conversation_history=history)
        assert result["final_response"].startswith(final)
        assert candidate not in str(result["messages"])
        assert candidate not in emitted
        assert diagnostic in str(db.get_messages_as_conversation(agent.session_id))
        assert not deferred.pop_notes(agent.session_id)
        history = result["messages"]
    assert len(calls) == 4  # LSP and recovery share the continuation, reset each turn.


@pytest.mark.parametrize("shared_budget", [1, 2])
def test_failed_edit_lsp_budget_is_durable_before_one_shot_exit(recovery_agent, tmp_path, shared_budget):
    from agent.lsp import deferred

    agent, db = recovery_agent
    agent.max_iterations = 1
    candidate = "Premature claim: both edits are complete"
    diagnostic = "other.py:1: a late LSP error"
    emitted, calls = [], []
    agent.interim_assistant_callback = lambda text, **kwargs: emitted.append(text)

    def summarize(messages, api_call_count):
        assert candidate not in str(messages)
        durable = db.get_messages_as_conversation(agent.session_id)
        assert diagnostic in str(durable)
        scaffolding = [m for m in messages if m.get("_file_mutation_recovery_synthetic")]
        assert len(scaffolding) == 2
        assert all(m["content"] not in [row.get("content") for row in durable] for m in scaffolding)
        return "Budget exhausted; edit failed"

    agent._handle_max_iterations = MagicMock(side_effect=summarize)

    def model_call(kwargs):
        calls.append(kwargs)
        agent.iteration_budget.max_total = shared_budget
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": str(tmp_path / "missing.md")},
            '{"error":"missing file"}', is_error=True,
        )
        deferred.post(agent.session_id, str(tmp_path / "other.py"), diagnostic, "original")
        return _response(candidate)

    agent._interruptible_api_call = model_call
    result = agent.run_conversation("Edit both files")
    assert result["final_response"].startswith("Budget exhausted; edit failed")
    assert candidate not in emitted
    assert candidate not in str(result["messages"])
    assert candidate not in str(db.get_messages_as_conversation(agent.session_id))
    agent._handle_max_iterations.assert_called_once()
    assert len(calls) == 1  # No next tool, stop boundary or user turn can rescue the note.
    reopened = SessionDB(db.db_path)
    try:
        durable = reopened.get_messages_as_conversation(agent.session_id)
    finally:
        reopened.close()
    assert diagnostic in str(durable)
    assert candidate not in str(durable)
    assert not any(m.get("_file_mutation_recovery_synthetic") for m in result["messages"])
    assert diagnostic in str(result["messages"])
    assert not deferred.pop_notes(agent.session_id)
    assert agent._cached_system_prompt == "stable test prompt"
    for transcript in (durable, result["messages"]):
        roles = [m["role"] for m in transcript]
        assert all(a != b for a, b in pairwise(roles))

    # Replay must use the durable note, not a surviving process-local LSP queue.
    def next_call(kwargs):
        assert diagnostic in str(kwargs["messages"])
        assert candidate not in str(kwargs["messages"])
        return _response("The LSP problem is now addressed")

    agent._interruptible_api_call = next_call
    agent.max_iterations = 4
    following = agent.run_conversation("Continue", conversation_history=durable)
    assert following["completed"]
    assert str(db.get_messages_as_conversation(agent.session_id)).count(diagnostic) == 1
    assert not deferred.pop_notes(agent.session_id)
