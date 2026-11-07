"""Telegram liveness pings never cancel, answer, steer or enqueue work."""
import time
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from tests.gateway.test_busy_session_ack import _make_adapter, _make_runner
from tools import clarify_gateway


def _busy_ping(mode="interrupt", text="Эй!"):
    runner, sentinel = _make_runner()
    runner._busy_input_mode = mode
    runner._queued_events = {}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group",
                           user_id="user1", thread_id="134")
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="ping")
    key = build_session_key(source)
    adapter = _make_adapter()
    agent = MagicMock()
    agent.get_activity_summary.return_value = {"current_tool": "mcp__shop__search", "seconds_since_activity": 0.0}
    runner.adapters[source.platform] = adapter
    runner._running_agents[key] = agent
    runner._running_agents_ts[key] = time.time() - 600
    queued = MessageEvent(text="existing follow-up", message_type=MessageType.TEXT, source=source)
    adapter._pending_messages[key] = queued
    return runner, adapter, agent, event, key, queued, sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["busy", "priority"])
@pytest.mark.parametrize("mode", ["interrupt", "queue", "steer"])
@pytest.mark.parametrize("text", ["Эй!", "  эЙ?!  ", "ping"])
async def test_ping_is_read_only(monkeypatch, path, mode, text):
    runner, adapter, agent, event, key, queued, _ = _busy_ping(mode, text)
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner._busy_ack_ts[key] = time.time()
    if path == "busy":
        assert await runner._handle_active_session_busy_message(event, key) is True
        sent = adapter._send_with_retry.call_args.kwargs
        assert sent["metadata"]["thread_id"] == "134"
        reply = sent["content"]
    else:
        reply = await runner._handle_message(event)
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()
    agent.redirect.assert_not_called()
    assert adapter._pending_messages == {key: queued}
    assert runner._queued_events == {}
    assert "mcp__shop__search" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarify", "approval", "starting", "send_error", "summary_error", "unauthorized"])
async def test_ping_special_states(monkeypatch, kind):
    from tools import approval
    runner, adapter, agent, event, key, queued, sentinel = _busy_ping()
    setup = {
        "clarify": lambda: clarify_gateway.register("ping-fixture", key, "Proceed?", ["Yes", "No"]),
        "approval": lambda: monkeypatch.setattr(approval, "has_blocking_approval", lambda key: True),
        "starting": lambda: runner._running_agents.__setitem__(key, sentinel),
        "send_error": lambda: setattr(adapter._send_with_retry, "side_effect", RuntimeError("synthetic transport error")),
        "summary_error": lambda: setattr(agent.get_activity_summary, "side_effect", RuntimeError("unavailable")),
        "unauthorized": lambda: setattr(runner, "_is_user_authorized", lambda source: False),
    }
    entry = setup[kind]()
    try:
        assert await runner._handle_active_session_busy_message(event, key) is True
        if entry is not None:
            assert not entry.event.is_set()
        expected = {"clarify": "Жду ответа", "approval": "Жду подтверждения", "starting": "запуска"}.get(kind)
        if expected:
            assert expected in adapter._send_with_retry.call_args.kwargs["content"]
        if kind == "approval":
            adapter.resume_typing_for_chat.assert_not_called()
        if kind == "unauthorized":
            agent.get_activity_summary.assert_not_called()
            adapter._send_with_retry.assert_not_called()
        agent.interrupt.assert_not_called()
        agent.steer.assert_not_called()
        assert adapter._pending_messages == {key: queued}
        assert runner._queued_events == {}
    finally:
        clarify_gateway.clear_session(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_non_answer_does_not_resolve_clarify(internal):
    runner, adapter, agent, event, key, queued, _ = _busy_ping(mode="queue")
    event.internal = internal
    if internal:
        event.text = "[ASYNC DELEGATION BATCH COMPLETE] synthetic result"
    entry = clarify_gateway.register("non-answer", key, "Proceed?", ["Yes", "No"])
    try:
        reply = await runner._handle_message(event)
        assert not entry.event.is_set()
        assert clarify_gateway.get_pending_for_session(key, include_choice_prompts=True) is entry
        if not internal:
            assert "Жду ответа" in reply
            assert adapter._pending_messages == {key: queued}
        agent.interrupt.assert_not_called()
        agent.steer.assert_not_called()
    finally:
        clarify_gateway.clear_session(key)


@pytest.mark.parametrize("kind", ["idle", "attachment", "other_platform", "internal", "untrusted", "instruction"])
def test_non_pings_are_not_intercepted(kind):
    runner, _, _, event, key, _, _ = _busy_ping()
    setup = {
        "idle": runner._running_agents.clear,
        "attachment": lambda: setattr(event, "media_urls", ["file:///fake/attachment.txt"]),
        "other_platform": lambda: setattr(event.source, "platform", Platform.DISCORD),
        "internal": lambda: setattr(event, "internal", True),
        "untrusted": lambda: setattr(event, "allow_gateway_control", False),
        "instruction": lambda: setattr(event, "text", "ping example.com"),
    }
    setup[kind]()
    assert runner._busy_status_ping_reply(event, key) is None
