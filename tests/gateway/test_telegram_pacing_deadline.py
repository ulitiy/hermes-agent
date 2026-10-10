"""Local queue waits must not become ambiguous network failures."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg


CONTENT = "## Fixture\n\n| Item | Value |\n|---|---|\n|x|1|"


def adapter(rich=False):
    instance = tg.TelegramAdapter(PlatformConfig(enabled=True, token="fixture", extra={
        "api_min_interval_seconds": 0.001, "rich_messages": rich,
    }))
    instance._bot = MagicMock()
    instance._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    if rich:
        instance._bot.do_api_request = AsyncMock(return_value={"message_id": 42})
    return instance


@pytest.mark.asyncio
async def test_pacing_wait_does_not_consume_network_deadline(monkeypatch):
    instance = adapter()
    monkeypatch.setattr(tg, "_TEXT_SEND_DEADLINE", 0.03)
    instance._bot_api_next_allowed_at["123"] = time.monotonic() + 0.60
    result = await instance.send("123", "fixture answer", metadata={"notify": True})
    assert result.success, result
    instance._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True])
async def test_edit_penalty_defers_send_without_attempt_or_ambiguous_timeout(monkeypatch, rich):
    from telegram.error import RetryAfter
    instance = adapter(rich)
    monkeypatch.setattr(tg, "_TEXT_SEND_DEADLINE", 0.03)
    edit = AsyncMock(side_effect=RetryAfter(60))
    with pytest.raises(RetryAfter):
        await instance._rate_limited_telegram_call("123", edit)
    result = await asyncio.wait_for(instance.send("123", CONTENT, metadata={"notify": True}), 1)
    assert not result.success and result.retry_after is not None and result.retry_after > 0, result
    assert result.error and result.error.startswith("flood_control:"), result
    instance._bot.send_message.assert_not_awaited()
    if rich:
        instance._bot.do_api_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True])
async def test_actual_network_deadline_is_still_ambiguous(monkeypatch, rich):
    instance = adapter(rich)
    monkeypatch.setattr(tg, "_TEXT_SEND_DEADLINE", 0.03)

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    request = instance._bot.do_api_request if rich else instance._bot.send_message
    request.side_effect = stalled
    result = await instance.send("123", CONTENT, metadata={"notify": True})
    assert not result.success and not result.retryable and result.retry_after is None, result
    assert result.error and "timed out" in result.error.lower(), result
    request.assert_awaited_once()
