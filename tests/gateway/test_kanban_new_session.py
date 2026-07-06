"""Kanban completions use the same opt-in New UX as ordinary turns."""
import asyncio
from unittest.mock import AsyncMock

from gateway import run
from gateway.config import Platform
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.gateway.test_kanban_notifier import RecordingAdapter, _make_runner, _run_one_notifier_tick


def test_completion_offers_new_on_the_delivery_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"session_reset": {"post_task_new_button": True}})
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="completed work", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", thread_id="8")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()
    adapter = RecordingAdapter()
    adapter.send_new_session_button = AsyncMock(return_value=None)
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    adapter.send_new_session_button.assert_awaited_once()
    assert adapter.send_new_session_button.call_args.kwargs["metadata"] == {"thread_id": "8"}


def test_completion_keeps_owning_adapter_and_dm_topic_metadata(monkeypatch):
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"session_reset": {"post_task_new_button": True}})
    default = RecordingAdapter()
    owner = RecordingAdapter()
    default.send_new_session_button = AsyncMock(return_value=None)
    owner.send_new_session_button = AsyncMock(return_value=None)
    runner = _make_runner(default)
    metadata = {"chat_type": "dm", "thread_id": "8", "direct_messages_topic_id": "8"}
    sent = asyncio.run(runner._offer_post_task_new_button_for_kanban_completion(
        adapter=owner, platform=Platform.TELEGRAM,
        sub={"chat_id": "chat-1", "thread_id": "8", "delivery_metadata": metadata}, msg="done",
    ))
    assert sent
    default.send_new_session_button.assert_not_awaited()
    owner.send_new_session_button.assert_awaited_once()
    assert owner.send_new_session_button.call_args.kwargs["metadata"] == metadata
