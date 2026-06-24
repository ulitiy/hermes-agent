"""Local transcript audit contract across the decomposed inbound paths."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent, MessageType, SendResult


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, False, True])
async def test_echo_opt_in_survives_real_loader_and_pending_dedup(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    text = "stt:\n  enabled: true\n"
    if setting is not None:
        text += f"  send_transcription: {str(setting).lower()}\n  send_transcription_header: 'Audit: '\n"
    (tmp_path / "config.yaml").write_text(text)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = load_gateway_config()
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="7")
    event = MessageEvent(source=source, text="", message_type=MessageType.VOICE, media_urls=["note.ogg"])
    runner._adapter_for_source = lambda _: adapter
    runner._thread_metadata_for_source = lambda *args: {"thread_id": "7"}
    runner._reply_anchor_for_event = lambda _: None
    runner._enrich_message_with_transcription = AsyncMock(return_value=('"hello"', ["hello"]))
    inbound = await runner._enrich_inbound_voice(event, source, "", ["note.ogg"])
    assert adapter.send.await_count == int(setting is True)
    response = "> 🎙 hello\n\nAnswer"
    assert runner._strip_leading_voice_transcript_from_response(response, inbound) == ("Answer" if setting else response)
    adapter.send.reset_mock()
    pending, _ = await runner._transcribe_and_echo_pending_voice(event, adapter, source, "", log_context="test")
    assert runner._strip_leading_voice_transcript_from_response(response, pending) == ("Answer" if setting else response)
    await runner._transcribe_and_echo_pending_voice(event, adapter, source, "", log_context="test")
    assert adapter.send.await_count == int(setting is True)
    if setting:
        adapter.send.assert_awaited_once_with("123", "Audit: > 🎙 hello", metadata={"thread_id": "7"})


def test_aliases_preserve_explicit_false_and_header():
    config = GatewayConfig.from_dict({"stt": {"echo_transcripts": True}})
    assert config.stt_send_transcription is True
    config = GatewayConfig.from_dict({"stt_send_transcription": False, "stt": {"echo_transcripts": True}})
    assert config.stt_send_transcription is False
    assert GatewayConfig.from_dict(config.to_dict()).stt_send_transcription is False
