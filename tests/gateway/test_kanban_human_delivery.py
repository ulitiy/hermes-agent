"""Exercise real notifier delivery with synthetic, unsanitized historical events."""
import asyncio

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from tests.gateway.test_kanban_notifier import RecordingAdapter, _make_runner, _run_one_notifier_tick


@pytest.mark.parametrize("mode", ["notify", "wake", "notify+wake"])
@pytest.mark.parametrize("kind", ["needs_input", "transient", None])
def test_escalation_delivery_keeps_reason_private_and_human_hold(tmp_path, monkeypatch, mode, kind):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **kw: None)
    secret = "sk-" + "S" * 48
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Synthetic " + secret, assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           delivery_mode=mode, thread_id="topic-1")
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            assert kb.block_task(conn, tid, kind=kind, reason="Choose destination " + secret)
            if kb.get_task(conn, tid).status == "triage":
                break
            assert kb.unblock_task(conn, tid)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    texts = [s["text"] for s in adapter.sent] + [e.text for e in adapter.handled]
    assert texts
    assert all(secret not in text for text in texts)
    if mode != "notify":
        assert adapter.handled
        text = adapter.handled[-1].text
        assert "Choose destination" in text
        assert "triage" in text and "Board:" in text
        if kind != "transient":
            assert "Do not unblock, resume, or decompose" in text
            assert "explicit human direction" in text
        else:
            assert "explicit human direction" not in text
    if mode != "wake":
        assert adapter.sent[-1]["metadata"]["thread_id"] == "topic-1"


@pytest.mark.parametrize("mode", ["notify", "wake", "notify+wake"])
def test_legacy_human_hold_is_not_silenced_as_diagnostic(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("display: {suppress_warning_notifications: true}\n")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **kw: None)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy owner decision", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", delivery_mode=mode)
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            assert kb.block_task(conn, tid, reason="Choose destination")
            if kb.get_task(conn, tid).status == "triage":
                break
            assert kb.unblock_task(conn, tid)
    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    if mode != "wake":
        assert adapter.sent and "Choose destination" in adapter.sent[-1]["text"]
    if mode != "notify":
        assert adapter.handled
        assert adapter.handled[-1].metadata["notification_category"] == "result"
