"""Topic roles are overlays, not transport-profile switches (rebase contract)."""

import os
from pathlib import Path

import yaml


def test_topic_overlay_follows_scoped_home_without_moving_transport(tmp_path, monkeypatch):
    from agent import secret_scope
    import gateway.run as gateway_run
    from gateway.config import Platform
    from gateway.run_topic_behavior import GatewayTopicBehaviorMixin
    from gateway.session import SessionSource, build_session_key
    from hermes_cli.config import get_config_path
    from hermes_constants import get_hermes_home, parse_reasoning_effort

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    homes = [tmp_path / "install-a", tmp_path / "install-b" / "profiles" / "transport-b"]
    expected = []
    for index, home in enumerate(homes):
        home.mkdir(parents=True)
        root = home.parent.parent if home.parent.name == "profiles" else home
        role_dir = root / "profiles" / "topic-role"
        role_dir.mkdir(parents=True)
        effort = ("low", "xhigh")[index]
        model = f"topic-model-{index}"
        url = f"https://topic-{index}.invalid/v1"
        profile_mapping = ("profile_by_thread", "topic_profile")[index]
        effort_mapping = ("reasoning_effort_by_thread", "thread_reasoning_effort")[index]
        (home / "config.yaml").write_text(yaml.safe_dump({
            "model": {"default": f"transport-model-{index}"},
            "telegram": {
                profile_mapping: {
                    "chat:7": {"profile": "${TOPIC_ROLE}"}, "7": "wrong-role",
                    "thread:8": {"name": "topic-role"}, "named topic": "topic-role",
                },
                effort_mapping: {"chat:7": {"effort": effort}, "7": "medium", "8": False},
            },
        }), encoding="utf-8")
        (role_dir / "config.yaml").write_text(yaml.safe_dump({
            "agent": {"reasoning_effort": effort},
            "model": {"default": model, "provider": "custom", "base_url": url,
                      "api_key": f"test-only-key-{index}", "api_mode": "chat_completions",
                      "max_tokens": 1234},
        }), encoding="utf-8")
        (role_dir / "SOUL.md").write_text(f"  Role in install {index}\n", encoding="utf-8")
        for folder in ("memories", "skills", "sessions"):
            (home / folder).mkdir()
            (home / folder / "owner.txt").write_text(str(index), encoding="utf-8")
        expected.append((role_dir, effort, model, url))

    monkeypatch.setattr(gateway_run, "_hermes_home", homes[0])
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    runner = GatewayTopicBehaviorMixin()
    for index in (0, 1, 0):
        home = homes[index]
        role_dir, effort, model, url = expected[index]
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", thread_id="7",
                               profile=f"transport-{index}")
        source_before = source.to_dict()
        key_before = build_session_key(source)
        with gateway_run._profile_runtime_scope(home, {"TOPIC_ROLE": "topic-role"}):
            role = runner._load_source_profile(source)
            assert role == "topic-role"
            assert runner._profile_dir(role) == role_dir
            assert runner._load_profile_soul(role) == f"Role in install {index}"
            assert runner._load_profile_reasoning_config(role) == parse_reasoning_effort(effort)
            assert runner._load_source_reasoning_config(source) == parse_reasoning_effort(effort)
            resolved_model, runtime = runner._load_profile_model_runtime_config(role)
            assert resolved_model == model
            assert runtime["provider"] == "custom"
            assert runtime["base_url"] == url
            assert runtime["api_key"] == f"test-only-key-{index}"
            assert runtime["api_mode"] == "chat_completions"
            assert runtime["max_tokens"] == 1234
            assert runner._load_profile_runtime_provider(role) == (resolved_model, runtime)
            assert source.to_dict() == source_before
            assert build_session_key(source) == key_before
            assert get_hermes_home() == home
            assert get_config_path() == home / "config.yaml"
            assert secret_scope.get_secret("TOPIC_ROLE") == "topic-role"
            assert os.environ["HERMES_HOME"] == str(homes[0])
            for folder in ("memories", "skills", "sessions"):
                assert (get_hermes_home() / folder / "owner.txt").read_text() == str(index)

            source.thread_id = "8"
            assert runner._load_source_profile(source) == role
            assert runner._load_source_reasoning_config(source) == {"enabled": False}
            source.thread_id = None
            source.chat_topic = "Named Topic"
            assert runner._load_source_profile(source) == role
            source.behavior_profile = "pinned-role"
            assert runner._load_source_profile(source) == "pinned-role"
            assert source.profile == f"transport-{index}"
            for missing in ("default", "../escape", "absent"):
                assert runner._profile_dir(missing) is None
                assert runner._load_profile_soul(missing) is None
                assert runner._load_profile_reasoning_config(missing) is None
                assert runner._load_profile_model_runtime_config(missing) == (None, None)
    assert runner._load_source_profile(None) is None
    assert runner._load_source_reasoning_config(None) is None


def test_voice_prefix_controls_only_one_turn_and_preserves_reply_context():
    from gateway.run_topic_behavior import GatewayTopicBehaviorMixin
    from hermes_constants import parse_reasoning_effort

    runner = GatewayTopicBehaviorMixin()
    base = parse_reasoning_effort("medium")
    assert runner._apply_turn_reasoning_override(base, None) is base
    for prefix, mode in (("LIGHT", "light"), ("lite", "light"), ("лайт", "light"),
                         ("heavy", "heavy"), ("хеви", "heavy"), ("хэви", "heavy")):
        speech = f'{prefix}: explain "this"\ncarefully'
        reply = '[Replying to a prior message]\nquoted context'
        wrapped = f'  [The user sent a voice message~ Here\'s what they said: "{speech}"]\n\n{reply}'
        assert runner._unwrap_voice_transcript(wrapped) == (speech, reply)
        parsed_mode, cleaned = runner._parse_turn_reasoning_prefix(wrapped)
        assert parsed_mode == mode
        assert cleaned == f'explain "this"\ncarefully\n\n{reply}'
        effort = "low" if mode == "light" else "xhigh"
        assert runner._apply_turn_reasoning_override(base, parsed_mode) == parse_reasoning_effort(effort)
        assert runner._parse_turn_reasoning_prefix(speech) == (mode, 'explain "this"\ncarefully')
        assert base == parse_reasoning_effort("medium")
    for untouched in ("light", "heavy:", "/heavy explain", "lighthouse shines", "the heavy box",
                      '[The user sent a voice message~ Here\'s what they said: "no prefix"]', None):
        assert runner._parse_turn_reasoning_prefix(untouched) == (None, untouched)
    assert runner._unwrap_voice_transcript("plain text") == (None, "")
    assert runner._unwrap_voice_transcript(None) == (None, "")
