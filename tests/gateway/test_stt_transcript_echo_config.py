"""STT audit copies are opt-in; legacy flags keep their precedence."""

import pytest
import yaml

from gateway.config import GatewayConfig, load_gateway_config
from gateway.run import GatewayRunner


@pytest.mark.parametrize("settings,enabled", [
    ({}, False),
    ({"stt": {"echo_transcripts": True}}, True),
    ({"stt_echo_transcripts": False, "stt": {"echo_transcripts": True}}, False),
    ({"stt": {"send_transcription": True, "echo_transcripts": False}}, True),
    ({"stt_send_transcription": False, "stt": {"send_transcription": True}}, False),
])
def test_stt_echo_config_roundtrip_and_runner(settings, enabled):
    cfg = GatewayConfig.from_dict(settings)
    assert cfg.stt_send_transcription is enabled
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig.from_dict(cfg.to_dict())
    assert runner._should_send_stt_transcription_echo() is enabled
    assert runner._should_echo_stt_transcripts() is enabled


def test_load_gateway_config_honors_legacy_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "stt": {"echo_transcripts": True}, "stt_echo_transcripts": False,
    }), encoding="utf-8")
    assert load_gateway_config().stt_send_transcription is False
