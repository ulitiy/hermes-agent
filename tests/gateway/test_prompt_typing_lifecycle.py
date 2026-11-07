"""Prompt completion releases the actual typing loop before the next long tool finishes."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter
from tests.waiting import async_wait_until
from tools import clarify_gateway as cm


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_path", ["button", "text", "timeout", "send_failure"])
async def test_prompt_releases_typing_before_next_tool(monkeypatch, reply_path):
    adapter = ProgressCaptureAdapter()
    loop = asyncio.get_running_loop()
    runner = object.__new__(TurnRunner)
    runner._approval_owner = None
    runner._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id="C1", _status_thread_metadata={"thread_id": "134"},
        session_key="typing-fixture", stream_consumer_holder=[None])
    runner._schedule = lambda coro, label: asyncio.run_coroutine_threadsafe(coro, loop)
    runner._close_native_stream_boundary = lambda *a, **kw: None
    monkeypatch.setattr(cm, "get_clarify_timeout", lambda: 0.03)

    async def send_clarify(**kwargs):
        assert "C1" in adapter._typing_paused
        if reply_path == "send_failure":
            return SendResult(success=False, error="synthetic delivery failure")
        if reply_path == "button":
            assert cm.resolve_gateway_clarify(kwargs["clarify_id"], "Buy")
        elif reply_path == "text":
            assert cm.resolve_text_response_for_session("typing-fixture", "1")
        return SendResult(success=True, message_id="prompt")

    adapter.send_clarify = send_clarify
    if reply_path == "send_failure":
        async def failed_send(*args, **kwargs):
            return SendResult(success=False, error="synthetic text failure")
        adapter.send = failed_send
    release_tool = threading.Event()
    after_prompt = threading.Event()
    result = []

    def worker():
        result.append(runner._clarify_callback_sync([
            {"qid": "q0", "question": "Proceed?", "choices": ["Buy", "Cancel"], "multi_select": False}]))
        after_prompt.set()
        assert release_tool.wait(5)

    stop = asyncio.Event()
    typing = asyncio.create_task(adapter._keep_typing(
        "C1", interval=0.01, metadata={"thread_id": "134"}, stop_event=stop))
    run = asyncio.create_task(asyncio.to_thread(worker))
    try:
        assert await asyncio.to_thread(after_prompt.wait, 3)
        assert "C1" not in adapter._typing_paused
        before = len(adapter.typing)
        await async_wait_until(lambda: len(adapter.typing) >= before + 2, timeout=1)
        assert not run.done()
        assert all(tick["metadata"] == {"thread_id": "134"} for tick in adapter.typing)
        expected = "submitted" if reply_path in {"button", "text"} else (
            "timed_out" if reply_path == "timeout" else "undelivered")
        assert result[0]["outcome"] == expected
    finally:
        release_tool.set()
        await asyncio.wait_for(run, 5)
        stop.set()
        await asyncio.wait_for(typing, 1)
        cm.clear_session("typing-fixture")
