"""File-recovery continuations must not become user history after semantic rebase."""

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
