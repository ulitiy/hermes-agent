"""Real loopback injection remains scoped and preserves Telegram topic/transport routing."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest


@pytest.mark.asyncio
async def test_injection_endpoint_is_scoped_authenticated_and_closes(tmp_path, monkeypatch):
    from agent import secret_scope
    from gateway.config import PlatformConfig
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    monkeypatch.setenv("HERMES_TELEGRAM_INJECT_SECRET", "wrong-launch-profile-secret")
    monkeypatch.setenv("HERMES_TELEGRAM_INJECT_PORT", "1")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()

    for index in (0, 1, 0):
        home = homes[index]
        secrets = {"HERMES_TELEGRAM_INJECT_SECRET": f"test-inject-{index}",
                   "HERMES_TELEGRAM_INJECT_PORT": "0"}
        received = asyncio.Queue()
        with _profile_runtime_scope(home, secrets):
            adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
            adapter._owner_profile = home.name
            adapter.send_typing = AsyncMock()
            adapter.stop_typing = AsyncMock()

            async def handler(event):
                await received.put((event, get_hermes_home()))

            adapter.set_message_handler(handler)
            try:
                await adapter._start_injection_server()
                assert adapter._injection_runner is not None
                address, = adapter._injection_runner.addresses
                assert address[0] == "127.0.0.1"
                url = f"http://127.0.0.1:{address[1]}/inject"
                async with aiohttp.ClientSession() as client:
                    headers = {"X-Inject-Secret": f"test-inject-{1 - index}"}
                    async with client.post(url, headers=headers, json={"chat_id": "c", "text": "x"}) as response:
                        assert response.status == 403
                    assert received.empty()
                    headers = {"X-Inject-Secret": secrets["HERMES_TELEGRAM_INJECT_SECRET"]}
                    async with client.post(url, headers=headers, data="{") as response:
                        assert response.status == 400
                    async with client.post(url, headers=headers, json={"chat_id": "c"}) as response:
                        assert response.status == 400
                    async with client.post(url, headers=headers, json={
                        "chat_id": "chat", "text": "voice transcript", "user_id": "user",
                        "message_thread_id": 7,
                    }) as response:
                        assert response.status == 200
                        assert await response.json() == {"status": "queued"}
                    event, event_home = await asyncio.wait_for(received.get(), timeout=5)
                    assert event_home == home
                    assert (event.text, event.source.chat_id, event.source.user_id) == ("voice transcript", "chat", "user")
                    assert (event.source.thread_id, event.source.chat_type, event.source.profile) == ("7", "group", home.name)
                    assert event.source._transport_adapter_ref() is adapter
                    await asyncio.gather(*list(adapter._background_tasks))
                    await adapter.disconnect()
                    assert adapter._injection_runner is None
                    with pytest.raises(aiohttp.ClientConnectorError):
                        await client.post(url, headers=headers, json={})
            finally:
                await adapter.disconnect()

    with _profile_runtime_scope(homes[0], {}):
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
        await adapter._start_injection_server()
        assert adapter._injection_runner is None
