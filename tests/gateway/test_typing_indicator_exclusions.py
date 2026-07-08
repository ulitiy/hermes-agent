import asyncio
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key


class _TypingTestAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return SendResult(success=True, message_id="sent")

    async def get_chat_info(self, chat_id):
        return {}


def _event(*, chat_id="-1003819053296", thread_id="3852"):
    return MessageEvent(
        text="ping",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="group",
            thread_id=thread_id,
        ),
    )


def _adapter(extra=None, *, typing_indicator=True):
    adapter = _TypingTestAdapter(
        PlatformConfig(
            enabled=True,
            token="fake",
            typing_indicator=typing_indicator,
            extra=extra or {},
        ),
        Platform.TELEGRAM,
    )
    adapter._message_handler = AsyncMock(return_value=None)
    keep_typing = AsyncMock()
    adapter._keep_typing = keep_typing  # type: ignore[method-assign]
    return adapter, keep_typing


@pytest.mark.asyncio
async def test_typing_indicator_disabled_thread_suppresses_refresh_loop():
    adapter, keep_typing = _adapter(
        {
            "typing_indicator_disabled_threads": [
                "-1003819053296:3852",
            ]
        }
    )
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    keep_typing.assert_not_called()


@pytest.mark.asyncio
async def test_typing_indicator_disabled_bare_thread_suppresses_refresh_loop():
    adapter, keep_typing = _adapter({"typing_indicator_disabled_threads": "3852"})
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    keep_typing.assert_not_called()


@pytest.mark.asyncio
async def test_typing_indicator_other_thread_still_refreshes():
    adapter, keep_typing = _adapter({"typing_indicator_disabled_threads": ["-1003819053296:3852"]})
    event = _event(thread_id="2")

    await adapter._process_message_background(event, build_session_key(event.source))
    await asyncio.sleep(0)

    keep_typing.assert_called_once()


@pytest.mark.asyncio
async def test_platform_wide_typing_indicator_false_still_wins():
    adapter, keep_typing = _adapter(typing_indicator=False)
    event = _event(thread_id="2")

    await adapter._process_message_background(event, build_session_key(event.source))

    keep_typing.assert_not_called()
