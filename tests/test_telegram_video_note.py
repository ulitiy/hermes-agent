"""Offline intake contracts: real PTB updates, filters, adapter and cache.

Only Telegram transport and the final dispatch boundary are fixtures. No bot,
STT provider or LLM is contacted, and cached payloads are synthetic test bytes.

Kept outside tests/gateway because that directory's conftest installs a
telegram MagicMock before collection, even when PTB is installed.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("telegram")
from telegram import Update
from telegram.ext import CallbackQueryHandler

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram import adapter as tg


PAYLOAD = b"synthetic-video-note-payload"


@pytest.fixture
def intake(tmp_path, monkeypatch):
    from gateway.platforms import base

    for name, directory in (
        ("VIDEO_CACHE_DIR", "videos"),
        ("AUDIO_CACHE_DIR", "audio"),
        ("DOCUMENT_CACHE_DIR", "documents"),
    ):
        monkeypatch.setattr(base, name, tmp_path / directory)
    file = SimpleNamespace(
        file_path="video/file.mp4",
        download_as_bytearray=AsyncMock(return_value=bytearray(PAYLOAD)),
    )
    bot = SimpleNamespace(
        id=999,
        username="test_bot",
        get_file=AsyncMock(return_value=file),
        send_message=AsyncMock(),
    )
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={
        "allow_from": ["123"],
        "allowed_chats": ["-100"],
        "group_allowed_chats": ["-100"],
        "allowed_topics": [],
        "require_mention": True,
        "observe_unmentioned_group_messages": False,
    }))
    adapter._bot = bot
    adapter.handle_message = AsyncMock()
    return adapter, bot, file


def _message(kind="video_note", *, forwarded=False, group=False, size=len(PAYLOAD)):
    attachment = {"file_id": "test-file", "file_unique_id": "test-unique", "file_size": size}
    attachment.update({
        "video_note": {"length": 240, "duration": 2},
        "video": {"width": 240, "height": 240, "duration": 2, "mime_type": "video/mp4"},
        "voice": {"duration": 2, "mime_type": "audio/ogg"},
        "audio": {"duration": 2, "mime_type": "audio/mpeg"},
        "document": {"file_name": "sample.pdf", "mime_type": "application/pdf"},
    }[kind])
    message = {
        "message_id": 10, "date": 1700000000,
        "from": {"id": 123, "is_bot": False, "first_name": "Tester"},
        "chat": {"id": -100, "type": "supergroup", "title": "Test group"} if group else {
            "id": 123, "type": "private", "first_name": "Tester",
        },
        kind: attachment,
    }
    if forwarded:
        message["forward_origin"] = {
            "type": "hidden_user", "sender_user_name": "Original sender", "date": 1699999999,
        }
    return message


def _update(bot, message):
    return Update.de_json({"update_id": 20, "message": message}, bot)


class _HandlersRegistered(BaseException):
    """Stop the sandbox bootstrap before initialization or polling."""


async def _dispatch(adapter, update, monkeypatch, callback_name="_handle_media_message"):
    handlers = []

    def add_handler(handler):
        handlers.append(handler)
        if isinstance(handler, CallbackQueryHandler):
            raise _HandlersRegistered

    app = SimpleNamespace(bot=adapter._bot, add_handler=add_handler)
    builder = Mock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.build.return_value = app
    with monkeypatch.context() as mp:
        mp.setattr(tg, "Application", SimpleNamespace(builder=lambda: builder))
        mp.setattr(tg, "HTTPXRequest", Mock())
        mp.setattr(tg, "discover_fallback_ips", AsyncMock(return_value=[]))
        mp.setattr(tg, "resolve_proxy_url", lambda *a, **kw: None)
        mp.setattr(adapter, "_fallback_ips", lambda: [])
        mp.setattr(adapter, "_acquire_platform_lock", lambda *a: True)
        mp.setattr(adapter, "_instrument_polling_request", lambda request: request)
        with pytest.raises(_HandlersRegistered):
            await adapter.connect()
    handler = next(h for h in handlers if h.callback == getattr(adapter, callback_name))
    assert handler.check_update(update), "registered PTB filter must accept this update"
    await handler.callback(update, SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [False, True])
async def test_circle_reaches_video_cache_through_registered_handler(intake, monkeypatch, forwarded):
    adapter, bot, file = intake
    update = _update(bot, _message(forwarded=forwarded))
    await _dispatch(adapter, update, monkeypatch)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.message_type == MessageType.VIDEO
    assert event.media_types == ["video/mp4"]
    path, = event.media_urls
    assert Path(path).suffix == ".mp4"
    assert Path(path).read_bytes() == PAYLOAD
    assert event.raw_message is update.message
    assert bool(event.raw_message.forward_origin) is forwarded
    bot.get_file.assert_awaited_once()
    file.download_as_bytearray.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("command", [False, True])
async def test_reply_to_circle_caches_video(intake, monkeypatch, forwarded, command):
    adapter, bot, file = intake
    message = _message()
    message.pop("video_note")
    message["text"] = "/help" if command else "Save this for later"
    if command:
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": 5}]
    message["reply_to_message"] = _message(forwarded=forwarded)
    message["message_id"] = 11
    adapter._enqueue_text_event = Mock()
    adapter._ensure_forum_commands = AsyncMock()
    await _dispatch(adapter, _update(bot, message), monkeypatch,
                    "_handle_command" if command else "_handle_text_message")

    sink = adapter.handle_message if command else adapter._enqueue_text_event
    sink.assert_called_once()
    event = sink.call_args.args[0]
    assert event.message_type == MessageType.VIDEO
    assert event.media_types == ["video/mp4"]
    path, = event.media_urls
    assert Path(path).read_bytes() == PAYLOAD
    assert path in event.text
    assert "Replied-to video" in event.text
    assert event.reply_to_message_id == "10"


@pytest.mark.asyncio
async def test_observed_circle_is_cached_without_dispatch(intake, monkeypatch):
    adapter, bot, file = intake
    adapter.config.extra["observe_unmentioned_group_messages"] = True
    store = Mock()
    store.get_or_create_session.return_value = SimpleNamespace(session_id="observed-test")
    adapter._session_store = store
    await _dispatch(adapter, _update(bot, _message(group=True, forwarded=True)), monkeypatch)

    adapter.handle_message.assert_not_called()
    store.append_to_transcript.assert_called_once()
    entry = store.append_to_transcript.call_args.args[1]
    assert entry["observed"] is True
    assert "video" in entry["content"].lower()
    # The cache is real; locate it from the patched cache directory, not a mock.
    from gateway.platforms.base import VIDEO_CACHE_DIR
    paths = list(VIDEO_CACHE_DIR.glob("*.mp4"))
    path, = paths
    assert path.read_bytes() == PAYLOAD
    assert str(path) in entry["content"]
    source = store.get_or_create_session.call_args.args[0]
    assert source.chat_id == "-100"
    assert source.user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("kind, expected_type, mime, note", [
    ("video_note", MessageType.VIDEO, "video/mp4", "video attachment"),
    ("video", MessageType.VIDEO, "video/mp4", "video attachment"),
    ("voice", MessageType.VOICE, "audio/ogg", "test transcript"),
    ("audio", MessageType.AUDIO, "audio/mp3", "audio file attachment"),
    ("document", MessageType.DOCUMENT, "application/pdf", "saved"),
])
async def test_intake_to_agent_text_only_voice_uses_stt(
    intake, monkeypatch, kind, expected_type, mime, note, forwarded,
):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from tools import transcription_tools

    adapter, bot, file = intake
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    runner._enrich_message_with_vision = AsyncMock()
    transcribe = Mock(return_value={"success": True, "transcript": "test transcript"})
    monkeypatch.setattr(transcription_tools, "transcribe_audio", transcribe)
    await _dispatch(adapter, _update(bot, _message(kind, forwarded=forwarded)), monkeypatch)
    event = adapter.handle_message.call_args.args[0]
    assert event.message_type == expected_type
    assert event.media_types == [mime]
    path, = event.media_urls
    assert Path(path).read_bytes() == PAYLOAD
    text = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])
    assert text is not None
    assert note in text.lower()
    if kind == "voice":
        transcribe.assert_called_once_with(path)
    else:
        transcribe.assert_not_called()
        assert path in text
    runner._enrich_message_with_vision.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["get_file", "download", "cache_limit"])
async def test_circle_failure_is_not_an_empty_success(intake, monkeypatch, failure):
    from gateway.platforms import base

    adapter, bot, file = intake
    if failure == "get_file":
        bot.get_file.side_effect = OSError("synthetic transport failure")
    elif failure == "download":
        file.download_as_bytearray.side_effect = OSError("synthetic transport failure")
    else:
        monkeypatch.setattr(base, "get_inbound_media_max_bytes", lambda: len(PAYLOAD) - 1)
    await _dispatch(adapter, _update(bot, _message()), monkeypatch)
    event = adapter.handle_message.call_args.args[0]
    assert event.media_urls == []
    assert event.media_types == []
    assert "could not be downloaded" in event.text
    bot.send_message.assert_awaited_once()
    assert not list(base.VIDEO_CACHE_DIR.glob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [None, 0, len(PAYLOAD)])
async def test_circle_accepts_unknown_or_boundary_size_like_video(intake, monkeypatch, size):
    adapter, bot, file = intake
    adapter._max_doc_bytes = len(PAYLOAD)
    await _dispatch(adapter, _update(bot, _message(size=size)), monkeypatch)
    assert adapter.handle_message.call_args.args[0].media_types == ["video/mp4"]


@pytest.mark.asyncio
async def test_oversized_circle_is_not_downloaded(intake, monkeypatch):
    adapter, bot, file = intake
    adapter._max_doc_bytes = len(PAYLOAD) - 1
    await _dispatch(adapter, _update(bot, _message()), monkeypatch)
    event = adapter.handle_message.call_args.args[0]
    assert event.media_urls == []
    assert "exceeds" in event.text.lower()
    bot.get_file.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["sender", "own_message", "mention", "chat", "group_allowlist", "topic", "ignored_topic"])
async def test_circle_respects_existing_intake_guards(intake, monkeypatch, guard):
    adapter, bot, file = intake
    message = _message(group=True)
    adapter.config.extra["observe_unmentioned_group_messages"] = guard != "mention"
    store = Mock()
    adapter._session_store = store
    if guard == "sender":
        message["from"]["id"] = 456
    elif guard == "own_message":
        message["from"]["id"] = bot.id
        adapter.config.extra["allow_from"] = [str(bot.id)]
    elif guard == "chat":
        adapter.config.extra["allowed_chats"] = ["-200"]
    elif guard == "group_allowlist":
        adapter.config.extra["group_allowed_chats"] = []
    elif guard in {"topic", "ignored_topic"}:
        message.update(message_thread_id=42, is_topic_message=True)
        message["chat"]["is_forum"] = True
        adapter.config.extra["allowed_topics" if guard == "topic" else "ignored_threads"] = [43 if guard == "topic" else 42]
    await _dispatch(adapter, _update(bot, message), monkeypatch)
    adapter.handle_message.assert_not_called()
    store.append_to_transcript.assert_not_called()
    bot.get_file.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_group_circle_reply_to_bot_is_a_trigger(intake, monkeypatch):
    adapter, bot, file = intake
    message = _message(group=True)
    message["reply_to_message"] = {
        "message_id": 9, "date": 1699999999, "chat": message["chat"],
        "from": {"id": bot.id, "is_bot": True, "first_name": "Bot"}, "text": "Send the video",
    }
    await _dispatch(adapter, _update(bot, message), monkeypatch)
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.call_args.args[0].media_types == ["video/mp4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("failure", ["get_file", "download", "cache_limit"])
async def test_referenced_circle_download_failure_is_not_silent(intake, monkeypatch, observed, failure):
    from gateway.platforms import base

    adapter, bot, file = intake
    if failure == "get_file":
        bot.get_file.side_effect = OSError("synthetic transport failure")
    elif failure == "download":
        file.download_as_bytearray.side_effect = OSError("synthetic transport failure")
    else:
        monkeypatch.setattr(base, "get_inbound_media_max_bytes", lambda: len(PAYLOAD) - 1)
    if observed:
        adapter.config.extra["observe_unmentioned_group_messages"] = True
        store = Mock()
        store.get_or_create_session.return_value = SimpleNamespace(session_id="observed-test")
        adapter._session_store = store
        await _dispatch(adapter, _update(bot, _message(group=True)), monkeypatch)
        text = store.append_to_transcript.call_args.args[1]["content"]
        adapter.handle_message.assert_not_called()
    else:
        message = _message()
        message.pop("video_note")
        message.update(text="Use this later", reply_to_message=_message())
        adapter._enqueue_text_event = Mock()
        adapter._ensure_forum_commands = AsyncMock()
        await _dispatch(adapter, _update(bot, message), monkeypatch, "_handle_text_message")
        event = adapter._enqueue_text_event.call_args.args[0]
        assert event.media_urls == []
        text = event.text
    assert "could not be downloaded" in text
    # Passive/replied media failures must not send unsolicited Telegram replies.
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [None, 0, len(PAYLOAD) + 1])
@pytest.mark.parametrize("observed", [False, True])
async def test_referenced_circle_requires_bounded_known_size(intake, monkeypatch, size, observed):
    adapter, bot, file = intake
    adapter._max_doc_bytes = len(PAYLOAD)
    circle = _message(group=observed, size=size)
    if observed:
        adapter.config.extra["observe_unmentioned_group_messages"] = True
        store = Mock()
        store.get_or_create_session.return_value = SimpleNamespace(session_id="observed-test")
        adapter._session_store = store
        await _dispatch(adapter, _update(bot, circle), monkeypatch)
        assert "too large or unverifiable" in store.append_to_transcript.call_args.args[1]["content"]
        adapter.handle_message.assert_not_called()
    else:
        message = _message()
        message.pop("video_note")
        message.update(text="Use this later", reply_to_message=circle)
        adapter._enqueue_text_event = Mock()
        adapter._ensure_forum_commands = AsyncMock()
        await _dispatch(adapter, _update(bot, message), monkeypatch, "_handle_text_message")
        assert adapter._enqueue_text_event.call_args.args[0].media_urls == []
    bot.get_file.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_reply_intake_guards_precede_download(intake, monkeypatch, authorized):
    adapter, bot, file = intake
    message = _message(group=True)
    message.pop("video_note")
    message.update(text="Use this later", reply_to_message=_message(group=True))
    if not authorized:
        message["from"]["id"] = 456
    adapter._enqueue_text_event = Mock()
    await _dispatch(adapter, _update(bot, message), monkeypatch, "_handle_text_message")
    adapter._enqueue_text_event.assert_not_called()
    bot.get_file.assert_not_called()
