"""Fallback delivery is deterministic, topic-scoped, and never part of user speech."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture
def voice(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True, stt_send_transcription=False)
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._model = "test-model"
    runner._base_url = ""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="group", thread_id="7")
    event = MessageEvent(text="", message_type=MessageType.VOICE, source=source,
                         media_urls=["/tmp/voice.ogg"], media_types=["audio/ogg"])
    notice = "⚠️ Распознавание: ElevenLabs → OpenAI (резерв)."
    response = {"success": True, "transcript": "Не сокращай мой запрос.", "provider": "openai",
                "fallback_from": "elevenlabs", "fallback_notice": notice}
    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", Mock(return_value=response))
    return runner, adapter, source, event, response


@pytest.mark.asyncio
async def test_normal_voice_notifies_origin_even_when_transcript_echo_is_off(voice):
    runner, adapter, source, event, response = voice
    text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    adapter.send.assert_awaited_once_with("123", response["fallback_notice"], metadata={"thread_id": "7"})
    assert response["transcript"] in text
    assert response["fallback_notice"] not in text


@pytest.mark.asyncio
async def test_pending_voice_notifies_event_origin(voice):
    runner, adapter, source, event, response = voice
    text, _ = await runner._transcribe_and_echo_pending_voice(
        event, adapter, source, "", log_context="test")
    again, _ = await runner._transcribe_and_echo_pending_voice(
        event, adapter, source, "", log_context="test")
    assert text == again
    adapter.send.assert_awaited_once_with("123", response["fallback_notice"], metadata={"thread_id": "7"})
    assert response["transcript"] in text
    assert response["fallback_notice"] not in text


@pytest.mark.asyncio
async def test_notice_delivery_failure_does_not_drop_transcript(voice):
    runner, adapter, source, event, response = voice
    adapter.send.side_effect = RuntimeError("messaging unavailable")
    text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert response["transcript"] in text
    assert "could not be transcribed" not in text


@pytest.mark.asyncio
async def test_primary_success_does_not_send_fallback_notice(voice):
    runner, adapter, source, event, response = voice
    response.pop("fallback_from")
    response.pop("fallback_notice")
    response["provider"] = "elevenlabs"
    text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert response["transcript"] in text
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_failure_notifies_without_claiming_a_transcript(voice):
    runner, adapter, source, event, response = voice
    response.update(success=False, transcript="", error="Primary 429; fallback timeout")
    text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    adapter.send.assert_awaited_once()
    assert "voice message could not be transcribed" in text
    assert "fallback timeout" not in text


@pytest.mark.asyncio
async def test_missing_source_does_not_broadcast_to_other_chats(voice):
    runner, adapter, _, _, response = voice
    _text, transcripts = await runner._enrich_message_with_transcription("", ["/tmp/voice.ogg"])
    assert transcripts == [response["transcript"]]
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_clarify_voice_notice_is_once_and_not_an_answer(voice):
    runner, adapter, _source, event, response = voice
    assert await runner._prepare_clarify_reply_text(event) == response["transcript"]
    assert await runner._prepare_clarify_reply_text(event) == response["transcript"]
    adapter.send.assert_awaited_once_with("123", response["fallback_notice"], metadata={"thread_id": "7"})
