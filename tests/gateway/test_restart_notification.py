"""Tests for /restart notification — the gateway notifies the requester on comeback."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)


# ── restart marker helpers ───────────────────────────────────────────────


def test_restart_notification_pending_true_with_claimed_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.claimed.json").write_text("{}")

    assert gateway_run._restart_notification_pending() is True



def test_planned_restart_notification_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".restart_pending.json"

    assert gateway_run._planned_restart_notification_pending() is False
    marker.write_text("{}")
    assert gateway_run._planned_restart_notification_pending() is True

    gateway_run._planned_restart_notification_path().unlink()

    assert gateway_run._planned_restart_notification_pending() is False


# ── _handle_restart_command writes .restart_notify.json ──────────────────


@pytest.mark.asyncio
async def test_restart_command_writes_notify_file(tmp_path, monkeypatch):
    """When /restart fires, the requester's routing info is persisted to disk."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    await runner._handle_restart_command(event)

    notify_path = tmp_path / ".restart_notify.json"
    assert notify_path.exists()
    data = json.loads(notify_path.read_text())
    assert data["platform"] == "telegram"
    assert data["chat_id"] == "42"
    assert data["chat_type"] == "dm"
    assert data["message_id"] == "m1"
    assert "thread_id" not in data  # no thread → omitted


@pytest.mark.asyncio
async def test_restart_command_uses_atomic_json_writes_for_marker_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    calls = []

    def _fake_atomic_json_write(path, payload, **kwargs):
        calls.append((Path(path).name, payload, kwargs))

    # _handle_restart_command lives in gateway/slash_commands.py (extracted from
    # run.py); it uses that module's top-level atomic_json_write import.
    import gateway.slash_commands as gateway_slash
    monkeypatch.setattr(gateway_slash, "atomic_json_write", _fake_atomic_json_write)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    await runner._handle_restart_command(event)

    names = [name for name, _payload, _kwargs in calls]
    assert names == [".restart_notify.json", ".restart_last_processed.json"]
    assert calls[0][1]["chat_id"] == "42"
    assert calls[1][1]["platform"] == "telegram"


@pytest.mark.asyncio
async def test_sethome_updates_running_config_for_same_process_restart(tmp_path, monkeypatch):
    """/sethome persists to env and updates in-memory config before restart."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    saved = {}

    def _fake_save_env_value(key, value):
        saved[key] = value

    monkeypatch.setattr("hermes_cli.config.save_env_value", _fake_save_env_value)
    monkeypatch.setattr("gateway.slash_commands.persist_home_channel", lambda home, **kwargs: None)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="home-42")
    source.chat_name = "Ops Home"
    event = MessageEvent(
        text="/sethome",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-home",
    )

    await runner._handle_set_home_command(event)

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    assert saved["TELEGRAM_HOME_CHANNEL"] == "home-42"
    assert home is not None
    assert home.chat_id == "home-42"
    assert home.name == "Ops Home"


@pytest.mark.asyncio
async def test_sethome_preserves_thread_target_for_same_process_restart(tmp_path, monkeypatch):
    """/sethome from a topic/thread stores the thread-aware home target."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    saved = {}

    def _fake_save_env_value(key, value):
        saved[key] = value

    monkeypatch.setattr("hermes_cli.config.save_env_value", _fake_save_env_value)
    monkeypatch.setattr("gateway.slash_commands.persist_home_channel", lambda home, **kwargs: None)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="parent-42", thread_id="topic-7")
    source.chat_name = "Ops Topic"
    event = MessageEvent(
        text="/sethome",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-home-thread",
    )

    await runner._handle_set_home_command(event)

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    assert saved["TELEGRAM_HOME_CHANNEL"] == "parent-42"
    assert saved["TELEGRAM_HOME_CHANNEL_THREAD_ID"] == "topic-7"
    assert home is not None
    assert home.chat_id == "parent-42"
    assert home.thread_id == "topic-7"


# ── home-channel startup notifications ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_home_channel_startup_notification_preserves_thread_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="parent-42",
        name="Ops Topic",
        thread_id="777",
    )
    # Declare the DM-topic lookup on the adapter CLASS, not the instance.
    # _is_telegram_dm_topic_target resolves _get_dm_topic_info via type(adapter)
    # so a MagicMock auto-attribute (instance-level) is intentionally ignored;
    # a real adapter exposes the method on its class. Mirrors the fake-adapter
    # pattern in test_telegram_topic_mode.py.
    class _DmTopicAdapter(type(adapter)):
        def _get_dm_topic_info(self, chat_id, thread_id):
            return {"name": "Ops Topic"}

    adapter.__class__ = _DmTopicAdapter
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="home"))

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("telegram", "parent-42", "777")}
    adapter.send.assert_called_once_with(
        "parent-42",
        "♻️ Gateway online — Hermes is back and ready.",
        metadata={
            "thread_id": "777",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "777",
        },
    )


@pytest.mark.asyncio
async def test_relay_fronted_logical_home_gets_startup_notification(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(return_value=SendResult(success=True, message_id="home"))
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(
            enabled=False,
            home_channel=HomeChannel(
                platform=Platform.SLACK,
                chat_id="D123",
                name="Owner DM",
                user_id="U123",
                scope_id="T123",
            ),
        ),
    }

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("slack", "D123", None)}
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[:3] == (
        Platform.SLACK,
        "D123",
        "♻️ Gateway online — Hermes is back and ready.",
    )
    assert relay.send_for_platform.await_args.kwargs["metadata"]["user_id"] == "U123"
    assert relay.send_for_platform.await_args.kwargs["metadata"]["scope_id"] == "T123"


# ── _send_restart_notification ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_restart_notification_uses_logical_platform_and_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps(
            {
                "platform": "slack",
                "chat_id": "D123",
                "chat_type": "dm",
                "user_id": "U123",
                "scope_id": "T123",
                "delivered_via_upstream_relay": True,
            }
        )
    )

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(
        return_value=SendResult(success=True, message_id="restart")
    )
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(enabled=False),
    }

    delivered_target = await runner._send_restart_notification()

    assert delivered_target == ("slack", "D123", None)
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[0:2] == (Platform.SLACK, "D123")
    metadata = relay.send_for_platform.await_args.kwargs["metadata"]
    assert metadata["user_id"] == "U123"
    assert metadata["scope_id"] == "T123"
    assert not notify_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_noop_when_no_file(tmp_path, monkeypatch):
    """Nothing happens if there's no pending restart notification."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock()

    await runner._send_restart_notification()

    adapter.send.assert_not_called()


@pytest.mark.asyncio
async def test_send_restart_notification_recovers_claimed_only_marker(tmp_path, monkeypatch):
    """A marker claimed by a crashed predecessor remains deliverable on boot."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    claimed_path = tmp_path / ".restart_notify.claimed.json"
    claimed_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="recovered"))

    delivered_target = await runner._send_restart_notification()

    assert delivered_target == ("telegram", "42", None)
    adapter.send.assert_awaited_once()
    assert not claimed_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_skips_when_adapter_missing(tmp_path, monkeypatch):
    """A marker for an unconfigured platform is a permanent cleanup decision."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "discord",  # runner only has telegram adapter
        "chat_id": "42",
    }))

    runner, _adapter = make_restart_runner()

    await runner._send_restart_notification()

    # File cleaned up even though we couldn't send
    assert not notify_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "secondary"])
async def test_send_restart_notification_retries_missing_configured_adapter(
    tmp_path, monkeypatch, profile
):
    """A configured adapter missing during reconnect keeps and later delivers."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    claimed_path = tmp_path / ".restart_notify.claimed.json"
    notify_path.write_text(json.dumps({
        "platform": "discord",
        "chat_id": "42",
        "profile": profile,
    }))

    runner, _adapter = make_restart_runner()
    runner.config.platforms[Platform.DISCORD] = PlatformConfig(enabled=True, token="***")
    runner._failed_platforms = {Platform.DISCORD: {"attempts": 0}}
    decoy = RestartTestAdapter()
    decoy.send = AsyncMock()
    if profile:
        runner._profile_adapters = {profile: {}}
        runner._profile_failed_platforms = {profile: {Platform.DISCORD: object()}}
        runner._failed_platforms = {}
        runner.adapters[Platform.DISCORD] = decoy
        runner.config.platforms[Platform.DISCORD].enabled = False
        runner.config.platforms[Platform.DISCORD].gateway_restart_notification = False

    assert await runner._send_restart_notification() is None
    assert not notify_path.exists()
    assert claimed_path.exists()

    reconnected = RestartTestAdapter()
    reconnected.platform = Platform.DISCORD
    reconnected.send = AsyncMock(return_value=SendResult(success=True, message_id="ready"))
    owner_adapters = runner._profile_adapters[profile] if profile else runner.adapters
    owner_adapters[Platform.DISCORD] = reconnected

    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=1,
    )

    reconnected.send.assert_awaited_once()
    decoy.send.assert_not_awaited()
    assert not claimed_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_cleans_up_missing_adapter_not_reconnecting(
    tmp_path, monkeypatch
):
    """Configured but non-retrying platforms are a terminal delivery decision."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    claimed_path = tmp_path / ".restart_notify.claimed.json"
    notify_path.write_text(json.dumps({
        "platform": "discord",
        "chat_id": "42",
    }))

    runner, _adapter = make_restart_runner()
    runner.config.platforms[Platform.DISCORD] = PlatformConfig(enabled=True, token="***")
    runner._failed_platforms = {}

    assert await runner._send_restart_notification() is None
    assert not notify_path.exists()
    assert not claimed_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_retains_and_retries_send_exception(
    tmp_path, monkeypatch
):
    """An unexpected transport exception remains durable for bounded retry."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    claimed_path = tmp_path / ".restart_notify.claimed.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(side_effect=[
        RuntimeError("network down"),
        SendResult(success=True, message_id="retry-ok"),
    ])

    delivered_target = await runner._send_restart_notification()

    assert delivered_target is None
    assert not notify_path.exists()
    assert claimed_path.exists()

    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=1,
    )

    assert adapter.send.await_count == 2
    assert not claimed_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_retains_retryable_sendresult_failure(
    tmp_path, monkeypatch
):
    """Transient startup failures keep the durable marker for a later retry."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(
            success=False,
            error="send_path_degraded",
            retryable=True,
        ),
    )

    delivered_target = await runner._send_restart_notification()

    assert delivered_target is None
    assert not notify_path.exists()
    assert (tmp_path / ".restart_notify.claimed.json").exists()


@pytest.mark.asyncio
async def test_send_restart_notification_serializes_concurrent_callers(
    tmp_path, monkeypatch
):
    """Concurrent callers on one runner must not send one claimed marker twice."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def _blocking_success(*_args, **_kwargs):
        send_started.set()
        await release_send.wait()
        return SendResult(success=True, message_id="one")

    adapter.send = AsyncMock(side_effect=_blocking_success)

    first = asyncio.create_task(runner._send_restart_notification())
    await send_started.wait()
    second = asyncio.create_task(runner._send_restart_notification())
    await asyncio.sleep(0)

    assert adapter.send.await_count == 1
    release_send.set()
    results = await asyncio.gather(first, second)

    assert adapter.send.await_count == 1
    assert sum(result is not None for result in results) == 1
    assert not (tmp_path / ".restart_notify.claimed.json").exists()


@pytest.mark.asyncio
async def test_restart_notification_watcher_retries_and_cleans_up(
    tmp_path, monkeypatch
):
    """The startup watcher retries a transient failure and delivers exactly once."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(side_effect=[
        SendResult(
            success=False,
            error="send_path_degraded",
            retryable=True,
        ),
        SendResult(success=True, message_id="restart-ok"),
    ])

    assert await runner._send_restart_notification() is None
    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=1,
    )

    assert adapter.send.await_count == 2
    assert not notify_path.exists()
    assert not (tmp_path / ".restart_notify.claimed.json").exists()


@pytest.mark.asyncio
async def test_restart_notification_watcher_drains_claimed_and_new_pending_markers(
    tmp_path, monkeypatch
):
    """A recovered claimed marker must not strand a newer restart marker."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    claimed_path = tmp_path / ".restart_notify.claimed.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "99",
    }))
    claimed_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(side_effect=[
        SendResult(
            success=False,
            error="send_path_degraded",
            retryable=True,
        ),
        SendResult(success=True, message_id="old-marker"),
        SendResult(success=True, message_id="new-marker"),
    ])

    assert await runner._send_restart_notification() is None
    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=1,
    )

    assert [call.args[0] for call in adapter.send.await_args_list] == ["42", "42", "99"]
    assert not notify_path.exists()
    assert not claimed_path.exists()


@pytest.mark.asyncio
async def test_restart_notification_watcher_stops_during_gateway_shutdown(
    tmp_path, monkeypatch
):
    """Shutdown preserves the marker instead of sending through torn-down adapters."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    runner._draining = True
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="late"))

    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=1,
    )

    adapter.send.assert_not_awaited()
    assert notify_path.exists()


@pytest.mark.asyncio
async def test_restart_notification_watcher_timeout_retains_marker(tmp_path, monkeypatch):
    """A bounded watcher timeout leaves durable work for the next startup."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock()

    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=0,
    )

    adapter.send.assert_not_awaited()
    assert notify_path.exists()


@pytest.mark.asyncio
async def test_restart_notification_watcher_deadline_retains_claimed_failures(
    tmp_path, monkeypatch
):
    """Repeated retryable sends remain durable when the watcher reaches deadline."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    claimed_path = tmp_path / ".restart_notify.claimed.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(
        success=False,
        error="send_path_degraded",
        retryable=True,
    ))

    await runner._watch_restart_notification(
        initial_delay=0,
        max_delay=0,
        timeout=0.02,
    )

    assert adapter.send.await_count >= 2
    assert not notify_path.exists()
    assert claimed_path.exists()


@pytest.mark.asyncio
async def test_restart_notification_scheduler_is_singleton(tmp_path, monkeypatch):
    """Startup paths share one watcher task and clear its lifecycle handle."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    watcher_started = asyncio.Event()
    release_watcher = asyncio.Event()

    async def _blocking_watcher():
        watcher_started.set()
        await release_watcher.wait()

    runner._watch_restart_notification = AsyncMock(side_effect=_blocking_watcher)

    runner._schedule_restart_notification_watch()
    first_task = runner._restart_notification_task
    runner._schedule_restart_notification_watch()
    await watcher_started.wait()

    runner._watch_restart_notification.assert_awaited_once()
    assert runner._restart_notification_task is first_task
    assert first_task in runner._background_tasks

    release_watcher.set()
    await first_task
    await asyncio.sleep(0)

    assert runner._restart_notification_task is None
    assert first_task not in runner._background_tasks


@pytest.mark.asyncio
async def test_send_restart_notification_cancellation_retains_claimed_marker(
    tmp_path, monkeypatch
):
    """Shutdown cancellation must not lose an in-flight durable notification."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    send_started = asyncio.Event()

    async def _blocking_send(*_args, **_kwargs):
        send_started.set()
        await asyncio.Event().wait()

    adapter.send = AsyncMock(side_effect=_blocking_send)

    task = asyncio.create_task(runner._send_restart_notification())
    await send_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not notify_path.exists()
    assert (tmp_path / ".restart_notify.claimed.json").exists()


@pytest.mark.asyncio
async def test_send_restart_notification_logs_warning_on_sendresult_failure(
    tmp_path, monkeypatch, caplog
):
    """Adapter that returns SendResult(success=False) must log a WARNING, not INFO.

    Regression guard: adapter.send() catches provider errors (e.g. Telegram
    "Chat not found") and returns SendResult(success=False) rather than
    raising. The caller previously ignored the return value and always
    logged "Sent restart notification to ..." at INFO — masking real
    delivery failures behind a fake success line.
    """
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=False, error="Chat not found"),
    )

    with caplog.at_level("DEBUG", logger="gateway.run"):
        delivered_target = await runner._send_restart_notification()

    assert delivered_target is None
    # Still cleans up.
    assert not notify_path.exists()




@pytest.mark.asyncio
async def test_shutdown_notifications_use_cached_live_thread_source_when_origin_missing():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="parent-42", chat_type="group", thread_id="topic-7")
    session_key = build_session_key(source)

    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=None)
    runner._cache_session_source(session_key, source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="shutdown"))

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_awaited_once()
    chat_id, message = adapter.send.await_args.args
    assert chat_id == "parent-42"
    assert message
    assert adapter.send.await_args.kwargs == {"metadata": {"thread_id": "topic-7", "_interim_send": True}}


@pytest.mark.asyncio
async def test_shutdown_home_channel_broadcast_carries_interim_marker():
    """Active-chat and home-channel shutdown sends are interim sends (#98432): a
    stream-is-the-message adapter must not seal an in-flight answer with the advisory,
    including when the home channel has no thread routing metadata."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="active-42", chat_type="dm")
    session_key = build_session_key(source)

    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))

    await runner._notify_active_sessions_of_shutdown()

    targets = [c.args[0] for c in adapter.send.await_args_list]
    assert targets == ["active-42", "home-42"]
    for call in adapter.send.await_args_list:
        assert call.kwargs["metadata"]["_interim_send"] is True


@pytest.mark.asyncio
async def test_shutdown_notifications_are_fully_muted_when_flag_disabled():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="active-42", chat_type="group", thread_id="topic-7")
    session_key = build_session_key(source)

    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    adapter.send = AsyncMock()

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_not_awaited()


