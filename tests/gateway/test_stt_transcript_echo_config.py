from pathlib import Path

from gateway.config import GatewayConfig, load_gateway_config
from gateway.run import GatewayRunner


def test_stt_send_transcription_defaults_off():
    cfg = GatewayConfig.from_dict({})

    assert cfg.stt_enabled is True
    assert cfg.stt_send_transcription is False
    assert cfg.to_dict()["stt_send_transcription"] is False


def test_legacy_stt_echo_transcripts_alias_enables_send_transcription():
    cfg = GatewayConfig.from_dict({"stt": {"enabled": True, "echo_transcripts": True}})

    assert cfg.stt_enabled is True
    assert cfg.stt_send_transcription is True


def test_top_level_legacy_stt_echo_transcripts_takes_precedence():
    cfg = GatewayConfig.from_dict({
        "stt_echo_transcripts": False,
        "stt": {"echo_transcripts": True},
    })

    assert cfg.stt_send_transcription is False


def test_load_gateway_config_honors_top_level_legacy_stt_echo_transcripts(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "stt:\n  echo_transcripts: true\nstt_echo_transcripts: false\n",
        encoding="utf-8",
    )

    cfg = load_gateway_config()

    assert cfg.stt_send_transcription is False


def test_gateway_runner_uses_stt_send_transcription_flag():
    runner = GatewayRunner.__new__(GatewayRunner)

    runner.config = GatewayConfig(stt_send_transcription=False)
    assert runner._should_send_stt_transcription_echo() is False
    assert runner._should_echo_stt_transcripts() is False

    runner.config = GatewayConfig(stt_send_transcription=True)
    assert runner._should_send_stt_transcription_echo() is True
    assert runner._should_echo_stt_transcripts() is True

    runner.config = GatewayConfig()
    assert runner._should_send_stt_transcription_echo() is False
    assert runner._should_echo_stt_transcripts() is False


def test_all_gateway_transcript_echo_sends_are_gated():
    source = Path(__file__).resolve().parents[2] / "gateway" / "run.py"
    lines = source.read_text().splitlines()

    echo_send_lines = [
        index
        for index, line in enumerate(lines)
        if "_format_stt_transcription_echo" in line and "def _format" not in line
    ]

    assert echo_send_lines
    for index in echo_send_lines:
        context = "\n".join(lines[max(0, index - 12): index + 1])
        assert "_should_send_stt_transcription_echo()" in context
