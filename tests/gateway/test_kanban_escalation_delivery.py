"""Real board escalation -> notifier/wake and decomposition guards (no LLM/network)."""
import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb, kanban_decompose as decomp, kanban_specify as specify


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb.init_db()
    return home


def escalate(conn):
    tid = kb.create_task(conn, title="stuck work", assignee="worker", session_id="creator")
    cursor = 0
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.claim_task(conn, tid, claimer="worker")
        if attempt == kb.BLOCK_RECURRENCE_LIMIT - 1:
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="test-chat",
                              thread_id="2", notifier_profile="default")
            cursor = max(ev.id for ev in kb.list_events(conn, tid))
            kb.advance_notify_cursor(conn, task_id=tid, platform="telegram", chat_id="test-chat",
                                     thread_id="2", new_cursor=cursor)
        assert kb.block_task(conn, tid, reason="approval needed: ghp_" + "A" * 36 + " +15555550123",
                             kind="needs_input")
        if attempt < kb.BLOCK_RECURRENCE_LIMIT - 1:
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid, cursor


class Adapter:
    def __init__(self, failure=None):
        self.failure = failure
        self.sent = []
        self.woken = []

    async def send(self, chat_id, text, metadata=None):
        if self.failure == "raise":
            raise RuntimeError("temporary transport failure")
        if self.failure == "result":
            return SendResult(success=False, error="temporary transport failure")
        self.sent.append((chat_id, text, metadata))
        return SendResult(success=True)

    async def handle_message(self, event):
        self.woken.append(event)


class Runner(GatewayRunner):
    def __init__(self, adapter):
        self._running = True
        self.adapters = {Platform.TELEGRAM: adapter}
        self._kanban_sub_fail_counts = {}


async def tick(monkeypatch, runner):
    async def stop(delay):
        if delay != 5:  # initial startup sleep
            runner._running = False
    runner._running = True
    with monkeypatch.context() as m:
        m.setattr(asyncio, "sleep", stop)
        await runner._kanban_notifier_watcher(interval=1)


@pytest.mark.parametrize("failure", [None, "raise", "result"])
@pytest.mark.parametrize("board_slug", ["default", "contractor-review-board"])
def test_escalation_delivered_to_thread_retry_retention_and_dedup(
    board, monkeypatch, failure, board_slug,
):
    # Exercise real board discovery and distinct DBs rather than a pinned DB alias.
    monkeypatch.delenv("HERMES_KANBAN_DB")
    kb.create_board(board_slug)
    with kb.connect_closing(board=board_slug) as conn:
        tid, cursor = escalate(conn)
    adapter = Adapter(failure)
    runner = Runner(adapter)
    asyncio.run(tick(monkeypatch, runner))
    if failure:
        with kb.connect_closing(board=board_slug) as conn:
            assert kb.list_notify_subs(conn)[0]["last_event_id"] == cursor
        assert adapter.woken == []
        adapter.failure = None
        asyncio.run(tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    chat, message, metadata = adapter.sent[0]
    assert chat == "test-chat" and metadata == {"thread_id": "2"}
    assert tid in message and "triage" in message and "human" in message.lower()
    if board_slug != "default":
        assert board_slug in message
    assert "approval needed" in message and "A" * 36 not in message
    assert "+15555550123" not in message
    assert len(adapter.woken) == 1
    wake = adapter.woken[0]
    assert wake.internal
    assert wake.source.thread_id == "2" and wake.source.profile == "default"
    assert wake.source.chat_id == "test-chat"
    assert tid in wake.text and board_slug in wake.text
    assert "do not" in wake.text.lower() and "human" in wake.text.lower()
    assert "A" * 36 not in wake.text
    assert "+15555550123" not in wake.text
    asyncio.run(tick(monkeypatch, runner))
    assert len(adapter.sent) == len(adapter.woken) == 1
    with kb.connect_closing(board=board_slug) as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert kb.list_notify_subs(conn)[0]["last_event_id"] > cursor
    if board_slug != "default":
        with kb.connect_closing(board="default") as conn:
            assert kb.get_task(conn, tid) is None
            assert kb.list_notify_subs(conn) == []


def test_escalated_card_never_enters_automatic_decomposition(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid, _ = escalate(conn)
        plain = kb.create_task(conn, title="new work", triage=True)
    assert tid not in decomp.list_triage_ids()
    assert plain in decomp.list_triage_ids()
    assert tid not in specify.list_triage_ids()
    aux = Mock(side_effect=AssertionError("escalation must not call a model"))
    monkeypatch.setattr("agent.auxiliary_client.get_text_auxiliary_client", aux)
    for fn in (decomp.decompose_task, specify.specify_task):
        result = fn(tid)
        assert not result.ok and "human" in result.reason
    aux.assert_not_called()
    with kb.connect_closing() as conn:
        assert not kb.specify_triage_task(conn, tid, title="overwrite", body="retry")
        assert kb.decompose_triage_task(conn, tid, root_assignee="worker",
                                       children=[{"title": "unwanted", "assignee": "worker"}]) is None
        assert kb.get_task(conn, tid).title == "stuck work"
        assert kb.get_task(conn, tid).status == "triage"
        assert len(kb.list_tasks(conn)) == 2
        assert kb.specify_triage_task(conn, plain, title="new spec")
