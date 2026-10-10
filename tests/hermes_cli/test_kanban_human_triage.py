"""Human-held escalation is not fresh work for the automatic specifier."""
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_graph as graph, kanban_decompose as decomp, kanban_specify as spec


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **kw: None)
    with kbc.connect_closing() as conn:
        yield conn


def escalate(conn, kind, tid=None):
    tid = tid or kb.create_task(conn, title="Fixture decision", assignee="worker")
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.block_task(conn, tid, kind=kind, reason="Choose the fixture destination")
        if kb.get_task(conn, tid).status == "triage":
            break
        assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    kb.add_comment(conn, tid, author="fixture", body="Additional context is not a decision")
    return tid


@pytest.mark.parametrize("kind", ["needs_input", None])
@pytest.mark.parametrize("operation", ["queue", "preflight", "specify", "decompose"])
def test_human_escalation_cannot_be_automatically_promoted(board, monkeypatch, kind, operation):
    tid = escalate(board, kind)
    auxiliary = Mock(side_effect=AssertionError("human-held card must not call a model"))
    monkeypatch.setattr(spec, "_call_aux", auxiliary)
    monkeypatch.setattr(decomp, "_call_aux", auxiliary)
    if operation == "queue":
        assert tid not in spec.list_triage_ids()
        assert tid not in decomp.list_triage_ids()
    elif operation == "preflight":
        assert not spec.specify_task(tid).ok
        assert not decomp.decompose_task(tid).ok
        auxiliary.assert_not_called()
    elif operation == "specify":
        assert not kb.specify_triage_task(board, tid, title="Unapproved rewrite")
    else:
        assert graph.decompose_triage_task(board, tid, root_assignee="worker", children=[
            {"title": "Unapproved child", "assignee": "worker"},
        ]) is None
        assert len(kb.list_tasks(board)) == 1
    assert kb.get_task(board, tid).status == "triage"


@pytest.mark.parametrize("kind", ["transient", "capability"])
def test_typed_technical_triage_remains_available(board, kind):
    tid = escalate(board, kind)
    assert tid in spec.list_triage_ids()
    assert tid in decomp.list_triage_ids()
    assert kb.specify_triage_task(board, tid, title="Technical repair")
    assert kb.get_task(board, tid).status == "ready"


def test_new_triage_remains_available(board):
    tid = kb.create_task(board, title="New task", triage=True, assignee="worker")
    assert tid in spec.list_triage_ids()
    assert kb.specify_triage_task(board, tid)


def test_human_holds_do_not_starve_the_decomposer_limit(board):
    for index in range(1000):
        tid = kb.create_task(board, title=f"Historical hold {index}", triage=True, assignee="worker", priority=10)
        board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?, ?, ?, ?)",
                      (tid, "block_loop_detected", '{"kind":"needs_input"}', 1))
        board.commit()
    fresh = kb.create_task(board, title="Eligible after holds", triage=True, assignee="worker", priority=0)
    assert decomp.list_triage_ids() == [fresh]


@pytest.mark.parametrize("fanout", [False, True])
def test_escalation_during_auxiliary_call_is_rejected_at_commit(board, monkeypatch, fanout):
    import json
    tid = kb.create_task(board, title="New task", triage=True, assignee="worker")

    def answer(*args, **kwargs):
        assert kb.specify_triage_task(board, tid)
        escalate(board, "needs_input", tid)
        return json.dumps({"fanout": fanout, "title": "No approval", "tasks": [
            {"title": "No approval child", "assignee": "worker"},
        ]}), ""

    monkeypatch.setattr(spec, "_call_aux", answer)
    monkeypatch.setattr(decomp, "_call_aux", answer)
    result = decomp.decompose_task(tid) if fanout else spec.specify_task(tid)
    assert not result.ok
    assert "triage" in result.reason
    task = kb.get_task(board, tid)
    assert task is not None and task.status == "triage"
    assert task.title == "New task"
    assert len(kb.list_tasks(board)) == 1
