"""Volatile Telegram ids belong to the user turn, inside the reply pointer."""
import pytest

from gateway.platforms.event import MessageEvent
from tests.gateway.test_reply_to_injection import _make_runner, _source


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_telegram_turn_includes_exact_trigger_id_inside_reply(reply):
    runner = _make_runner()
    source = _source()
    event = MessageEvent(text="hello", source=source, message_id="12345",
                         reply_to_message_id="42" if reply else None,
                         reply_to_text="previous" if reply else None)
    result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert "[Telegram message id: `12345`" in result
    assert result.endswith("hello")
    if reply:
        assert result.startswith('[Replying to: "previous"]')
    else:
        assert result.startswith("[Telegram message id:")
