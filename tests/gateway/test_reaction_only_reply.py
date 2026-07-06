"""Reaction-only gateway control replies.

The model may emit a whole-response marker like ``REACTION_ONLY: 👍`` to ask
Telegram/Sprout to acknowledge a message with a reaction instead of a visible
text bubble.  This must behave like ``NO_REPLY`` at the transcript boundary
(the marker is persisted by the runner) but with a platform side effect at the
delivery boundary.
"""

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _ReactionOnlyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake", typing_indicator=False),
            Platform.TELEGRAM,
        )
        self.sent: list[dict] = []
        self.reactions: list[dict] = []
        self.completed: list[ProcessingOutcome] = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return SendResult(success=True, message_id="text-1")

    async def send_typing(self, chat_id: str, metadata=None):
        return None

    async def send_reaction_only_response(self, event: MessageEvent, emoji: str) -> SendResult:
        self.reactions.append({
            "chat_id": event.source.chat_id,
            "message_id": event.message_id,
            "emoji": emoji,
        })
        return SendResult(success=True, message_id=event.message_id)

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        self.completed.append(outcome)


def _make_event() -> MessageEvent:
    return MessageEvent(
        text="понял?",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="111",
            chat_type="group",
            user_id="42",
        ),
        message_id="m1",
    )


@pytest.mark.asyncio
async def test_reaction_only_marker_sets_reaction_without_sending_text():
    adapter = _ReactionOnlyAdapter()

    async def handler(_event):
        return "REACTION_ONLY: 👍"

    adapter.set_message_handler(handler)
    event = _make_event()

    await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.sent == []
    assert adapter.reactions == [{"chat_id": "111", "message_id": "m1", "emoji": "👍"}]
    assert event.metadata["_hermes_reaction_only_response"] == "👍"
    assert adapter.completed == [ProcessingOutcome.SUCCESS]


@pytest.mark.asyncio
async def test_reaction_only_alias_is_normalized_before_delivery():
    adapter = _ReactionOnlyAdapter()

    async def handler(_event):
        return "REACTION_ONLY: ✅"

    adapter.set_message_handler(handler)
    event = _make_event()

    await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.sent == []
    assert adapter.reactions == [{"chat_id": "111", "message_id": "m1", "emoji": "👍"}]
