from __future__ import annotations

from datetime import datetime

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run_new_session import NewSessionPolicy
from gateway.session import SessionSource, SessionStore


def test_get_reset_policy_prefers_profile_override():
    cfg = {
            "session_reset": {"mode": "none"},
            "reset_by_profile": {
                "admin": {
                    "mode": "always",
                    "notify": False,
                    "post_task_new_button": True,
                }
            },
        }

    policy = NewSessionPolicy.from_config(cfg, profile="admin")

    assert policy.mode == "always"
    assert policy.notify is False
    assert policy.post_task_new_button is True


@pytest.mark.parametrize(
    "profile_kwargs",
    [
        {"profile": "task-manager"},
        {"behavior_profile": "task-manager"},
        {"profile": "transport", "behavior_profile": "task-manager"},
    ],
    ids=["transport-profile", "behavior-profile", "behavior-over-transport"],
)
def test_profile_always_reset_policy_starts_each_turn_fresh(tmp_path, monkeypatch, profile_kwargs):
    from gateway.run import GatewayRunner, _profile_runtime_scope

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "session_reset:\n  by_profile:\n    task-manager:\n      mode: always\n      notify: false\n"
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    cfg = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=cfg)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="forum",
        thread_id="107",
        user_id="42",
        **profile_kwargs,
    )

    with _profile_runtime_scope(tmp_path, {}):
        policy = runner._load_new_session_policy(source)
    assert policy.mode == "always"

    first = store.get_or_create_session(source)
    first.last_prompt_tokens = 123
    first.updated_at = datetime.now()
    store._save()

    second = store.get_or_create_session(source, reset_policy=policy)

    assert second.session_id != first.session_id
    assert second.was_auto_reset is True
    assert second.auto_reset_reason == "always"
    assert second.reset_had_activity is True
    # Internal wakes and post-turn bookkeeping cannot trigger another boundary.
    assert store.get_or_create_session(source).session_id == second.session_id
    assert store.get_or_create_session(source, touch_activity=False, reset_policy=policy).session_id == second.session_id


def test_local_idle_policy_uses_transport_scope_and_preserves_internal_work(tmp_path, monkeypatch):
    from datetime import timedelta
    from pathlib import Path
    import yaml
    import gateway.run as gateway_run
    from agent import secret_scope
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    homes = [tmp_path / "transport-a", tmp_path / "transport-b"]
    for i, home in enumerate(homes):
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({
            "telegram": {"profile_by_thread": {"room:7": "admin"}},
            "session_reset": {"by_profile": {"admin": {
                "mode": "idle", "idle_minutes": 30 if i == 0 else 90,
                "post_task_new_button": i == 0,
            }}},
        }))
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    for i in (0, 1, 0):
        with gateway_run._profile_runtime_scope(homes[i], {}):
            source = SessionSource(platform=Platform.TELEGRAM, chat_id="room", thread_id="7", profile="transport")
            before = source.to_dict()
            policy = runner._load_new_session_policy(source)
            assert policy.idle_minutes == (30 if i == 0 else 90)
            assert policy.post_task_new_button is (i == 0)
            store = SessionStore(homes[i] / "sessions", GatewayConfig())
            first = store.get_or_create_session(source)
            first.updated_at = datetime.now() - timedelta(minutes=60)
            store._save()
            # Background delivery cannot reset or extend the activity clock.
            assert store.get_or_create_session(source, touch_activity=False, reset_policy=policy).session_id == first.session_id
            second = store.get_or_create_session(source, reset_policy=policy)
            assert (second.session_id != first.session_id) is (i == 0)
            second.updated_at = datetime.now() - timedelta(days=1)
            second.resume_pending = True
            store._save()
            assert store.get_or_create_session(source, reset_policy=policy).session_id == second.session_id
            store.clear_resume_pending(second.session_key)
            assert source.to_dict() == before
            assert get_hermes_home() == homes[i]
