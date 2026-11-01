"""Exercise the real runner's prompt wait and typing loop with fake I/O only."""

import asyncio
import sys
import threading
from types import ModuleType

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner
from tools import clarify_gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_path", ["button", "text", "timeout", "send_failure"])
async def test_clarify_releases_pause_before_long_tool_wait(monkeypatch, tmp_path, reply_path):
    """Same owner loop must refresh again while the worker waits, not just at final delivery."""
    import gateway.run as gateway_run

    chat_id = "-1001"
    session_key = "agent:main:telegram:group:-1001:134"
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="group", thread_id="134",
    )
    adapter = ProgressCaptureAdapter()
    after_prompt = threading.Event()
    release_tool = threading.Event()
    responses = []
    pause_after_prompt = []

    async def send_clarify(**kwargs):
        assert chat_id in adapter._typing_paused
        if reply_path == "send_failure":
            return SendResult(success=False, error="fake delivery failure")
        if reply_path == "button":
            assert clarify_gateway.resolve_gateway_clarify(kwargs["clarify_id"], "Buy")
        elif reply_path == "text":
            assert clarify_gateway.resolve_text_response_for_session(session_key, "1")
        return SendResult(success=True, message_id="prompt")

    adapter.send_clarify = send_clarify

    class PromptAgent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, conversation_history=None, task_id=None):
            responses.append(self.clarify_callback("Proceed?", ["Buy", "Cancel"]))
            pause_after_prompt.append(chat_id in adapter._typing_paused)
            after_prompt.set()
            # Models a slow synchronous MCP call: event-loop typing must remain
            # independent of tool completion. No provider/MCP/Telegram transport.
            assert release_tool.wait(5), "test failed to release fake tool"
            return {"final_response": "done", "messages": [], "api_calls": 1}

    fake_agent_module = ModuleType("run_agent")
    fake_agent_module.AIAgent = PromptAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_agent_module)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 0 if reply_path == "timeout" else 1)
    runner = _make_runner(adapter)
    stop = asyncio.Event()
    typing = asyncio.create_task(adapter._keep_typing(chat_id, interval=0.01, metadata={"thread_id": "134"}, stop_event=stop))
    run = asyncio.create_task(runner._run_agent(
        message="purchase", context_prompt="", history=[], source=source,
        session_id="test-prompt-typing", session_key=session_key,
    ))
    try:
        assert await asyncio.to_thread(after_prompt.wait, 3)
        assert pause_after_prompt == [False]
        before = len(adapter.typing)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(adapter.typing) >= before + 2:
                break
        assert len(adapter.typing) >= before + 2
        assert not run.done(), "typing must resume before the slow tool finishes"
        assert all(tick["metadata"] == {"thread_id": "134"} for tick in adapter.typing)
        if reply_path in {"button", "text"}:
            assert responses == ["Buy"]
        elif reply_path == "send_failure":
            assert responses == ["[clarify prompt could not be delivered]"]
        else:
            assert responses == ["[user did not respond within 0m]"]
    finally:
        release_tool.set()
        await asyncio.wait_for(run, timeout=5)
        stop.set()
        await asyncio.wait_for(typing, timeout=1)
        clarify_gateway.clear_session(session_key)
    assert chat_id not in adapter._typing_paused
