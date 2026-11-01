"""A short Telegram liveness check is not an instruction to cancel/steer/queue."""

import time
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from tests.gateway.test_busy_session_ack import _make_adapter, _make_runner


def _busy_ping(mode="interrupt", text="Эй!"):
    runner, sentinel = _make_runner()
    runner._busy_input_mode = mode
    runner._queued_events = {}
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group",
        user_id="user1", thread_id="134",
    )
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="ping")
    key = build_session_key(source)
    adapter = _make_adapter()
    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "current_tool": "mcp__shop__search", "seconds_since_activity": 0.0,
    }
    runner.adapters[source.platform] = adapter
    runner._running_agents[key] = agent
    runner._running_agents_ts[key] = time.time() - 600
    # Existing work in the queue must not be overwritten or added to by a ping.
    queued = MessageEvent(text="existing follow-up", message_type=MessageType.TEXT, source=source)
    adapter._pending_messages[key] = queued
    return runner, adapter, agent, event, key, queued, sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["busy", "priority"])
@pytest.mark.parametrize("mode", ["interrupt", "queue", "steer"])
@pytest.mark.parametrize("text", ["Эй!", "  эЙ?!  ", "ping"])
async def test_status_ping_reports_live_tool_without_changing_work(monkeypatch, path, mode, text):
    runner, adapter, agent, event, key, queued, _ = _busy_ping(mode, text)
    # Explicit status checks must still get feedback when ordinary busy acks
    # are muted or have just fired. They are not another follow-up instruction.
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner._busy_ack_ts[key] = time.time()
    if path == "busy":
        assert await runner._handle_active_session_busy_message(event, key) is True
        adapter._send_with_retry.assert_awaited_once()
        sent = adapter._send_with_retry.call_args.kwargs
        reply = sent["content"]
        assert sent["metadata"]["thread_id"] == "134"
    else:
        reply = await runner._handle_message(event)
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()
    assert adapter._pending_messages == {key: queued}
    assert runner._queued_events == {}
    assert isinstance(reply, str)
    assert "mcp__shop__search" in reply


@pytest.mark.asyncio
async def test_starting_status_does_not_enqueue_ping():
    runner, adapter, _, event, key, queued, sentinel = _busy_ping()
    runner._running_agents[key] = sentinel
    assert await runner._handle_active_session_busy_message(event, key) is True
    assert adapter._pending_messages == {key: queued}
    assert runner._queued_events == {}
    assert "запуска" in adapter._send_with_retry.call_args.kwargs["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Эй, остановись!", "ping example.com", "stop"])
async def test_real_followup_is_not_swallowed_as_status(text, monkeypatch):
    runner, adapter, agent, event, key, _, _ = _busy_ping(text=text)
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    await runner._handle_active_session_busy_message(event, key)
    agent.interrupt.assert_called_once_with(text)


@pytest.mark.asyncio
async def test_unauthorized_ping_cannot_read_activity():
    runner, adapter, agent, event, key, _, _ = _busy_ping()
    runner._is_user_authorized = lambda source: False
    assert await runner._handle_active_session_busy_message(event, key) is True
    agent.get_activity_summary.assert_not_called()
    adapter._send_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_internal_ping_is_not_user_status():
    runner, adapter, agent, event, key, queued, _ = _busy_ping()
    event.internal = True
    assert await runner._handle_active_session_busy_message(event, key) is False
    agent.get_activity_summary.assert_not_called()
    adapter._send_with_retry.assert_not_called()
    assert adapter._pending_messages == {key: queued}


@pytest.mark.asyncio
async def test_status_delivery_failure_never_falls_through_to_interrupt():
    runner, adapter, agent, event, key, queued, _ = _busy_ping()
    adapter._send_with_retry.side_effect = RuntimeError("fake transport failure")
    assert await runner._handle_active_session_busy_message(event, key) is True
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()
    assert adapter._pending_messages == {key: queued}
    assert runner._queued_events == {}


@pytest.mark.asyncio
async def test_ping_while_approval_pending_only_reports_wait(monkeypatch):
    from tools import approval

    runner, adapter, agent, event, key, queued, _ = _busy_ping()
    monkeypatch.setattr(approval, "has_blocking_approval", lambda key: True)
    assert await runner._handle_active_session_busy_message(event, key) is True
    assert "Жду подтверждения" in adapter._send_with_retry.call_args.kwargs["content"]
    agent.interrupt.assert_not_called()
    adapter.resume_typing_for_chat.assert_not_called()
    assert adapter._pending_messages == {key: queued}
    assert runner._queued_events == {}


@pytest.mark.asyncio
async def test_ping_summary_failure_still_reports_without_interrupt():
    runner, adapter, agent, event, key, queued, _ = _busy_ping()
    agent.get_activity_summary.side_effect = RuntimeError("summary unavailable")
    assert await runner._handle_active_session_busy_message(event, key) is True
    adapter._send_with_retry.assert_awaited_once()
    agent.interrupt.assert_not_called()
    assert adapter._pending_messages == {key: queued}


@pytest.mark.parametrize("kind", ["idle", "attachment", "other_platform"])
def test_status_ping_excludes_non_busy_or_non_text_requests(kind):
    runner, _, _, event, key, _, _ = _busy_ping()
    if kind == "idle":
        runner._running_agents.clear()
    elif kind == "attachment":
        event.media_urls = ["file:///fake/attachment.txt"]
    else:
        event.source.platform = Platform.DISCORD
    assert runner._busy_status_ping_reply(event, key) is None
