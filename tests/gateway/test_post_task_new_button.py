from __future__ import annotations


import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.run_new_session import NewSessionPolicy
from gateway.session import SessionSource


class FakeTelegramAdapter:
    def __init__(self):
        self.registered = None
        self.sent = []

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.registered = (session_key, callback, generation)

    async def send_new_session_button(self, chat_id, *, text, button_label, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "button_label": button_label,
                "metadata": metadata,
            }
        )


def _runner_with(config: dict, adapter: FakeTelegramAdapter | None = None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._load_new_session_policy = lambda source: NewSessionPolicy.from_config(config, source.behavior_profile or source.profile)
    setattr(runner, "adapters", {Platform.TELEGRAM: adapter or FakeTelegramAdapter()})
    return runner


@pytest.mark.asyncio
async def test_completed_telegram_turn_registers_post_delivery_new_button():
    adapter = FakeTelegramAdapter()
    runner = _runner_with(
        {"session_reset": {"post_task_new_button": True}},
        adapter,
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="forum",
        thread_id="2",
        user_id="42",
    )

    runner._register_post_task_new_button(
        source=source,
        session_key="telegram:-1001:2",
        run_generation=7,
        agent_result={"final_response": "Done", "completed": True},
    )

    assert adapter.registered is not None
    session_key, callback, generation = adapter.registered
    assert session_key == "telegram:-1001:2"
    assert generation == 7

    await callback()

    assert adapter.sent == [
        {
            "chat_id": "-1001",
            "text": "Готово. Новая тема?",
            "button_label": "New",
            "metadata": {"thread_id": "2"},
        }
    ]


@pytest.mark.asyncio
async def test_streamed_turn_sends_post_task_new_button_immediately():
    adapter = FakeTelegramAdapter()
    runner = _runner_with(
        {"session_reset": {"post_task_new_button": True}},
        adapter,
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="forum",
        thread_id="2",
        user_id="42",
    )

    sent = await runner._send_post_task_new_button_now(
        source=source,
        agent_result={"final_response": "Done", "completed": True, "already_sent": True},
        reply_to_message_id="99",
    )

    assert sent is True
    assert adapter.sent == [
        {
            "chat_id": "-1001",
            "text": "Готово. Новая тема?",
            "button_label": "New",
            "metadata": {"thread_id": "2"},
        }
    ]


@pytest.mark.parametrize(
    "profile_kwargs",
    [
        {"profile": "admin"},
        {"behavior_profile": "admin"},
    ],
    ids=["transport-profile", "behavior-profile"],
)
def test_profile_always_fresh_policy_suppresses_post_task_new_button(profile_kwargs):
    runner = _runner_with(
        {"session_reset": {"post_task_new_button": True, "by_profile": {
            "admin": {"mode": "always", "notify": False, "post_task_new_button": False},
        }}}
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="forum",
        thread_id="1",
        **profile_kwargs,
    )

    assert runner._post_task_new_button_policy(
        source,
        {"final_response": "Done", "completed": True},
    ) is None
