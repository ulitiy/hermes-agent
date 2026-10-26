"""Explicit STT fallback is opt-in and preserves the original audio/model boundary."""
import wave
from unittest.mock import Mock

import pytest

from tools import transcription_tools as stt


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 1600)
    return str(path)


@pytest.fixture
def providers(monkeypatch):
    config = {
        "enabled": True,
        "provider": "elevenlabs",
        "fallback_provider": "openai",
        "elevenlabs": {"model": "scribe_v2"},
        "openai": {"model": "whisper-1"},
    }
    monkeypatch.setattr(stt, "_load_stt_config", lambda: config)
    monkeypatch.setattr(stt, "_get_provider", lambda cfg: cfg["provider"])
    primary = Mock(return_value={"success": False, "transcript": "", "error": "HTTP 429"})
    backup = Mock(return_value={"success": True, "transcript": "Полный исходный запрос.", "provider": "openai"})
    monkeypatch.setattr(stt, "_transcribe_elevenlabs", primary)
    monkeypatch.setattr(stt, "_transcribe_openai", backup)
    return config, primary, backup


def test_explicit_fallback_after_provider_failure_uses_same_audio_and_own_model(audio, providers):
    config, primary, backup = providers
    result = stt.transcribe_audio(audio, model="scribe_v2")
    assert result["success"] is True
    assert result["transcript"] == "Полный исходный запрос."
    assert result["provider"] == "openai"
    assert result["fallback_from"] == "elevenlabs"
    assert "elevenlabs" in result["fallback_notice"]
    assert "openai" in result["fallback_notice"]
    primary.assert_called_once_with(audio, "scribe_v2")
    backup.assert_called_once_with(audio, "whisper-1")
    assert config["provider"] == "elevenlabs"


@pytest.mark.parametrize("fallback", [None, "", "none", "elevenlabs", ["openai"]])
def test_without_explicit_distinct_fallback_no_other_provider_runs(audio, providers, fallback):
    config, primary, backup = providers
    config["fallback_provider"] = fallback
    result = stt.transcribe_audio(audio)
    assert result["success"] is False
    assert "fallback_from" not in result
    primary.assert_called_once()
    backup.assert_not_called()


def test_primary_success_is_unchanged_and_silent(audio, providers):
    _, primary, backup = providers
    primary.return_value = {"success": True, "transcript": "Основной работает.", "provider": "elevenlabs"}
    assert stt.transcribe_audio(audio) == primary.return_value
    backup.assert_not_called()


def test_both_fail_only_one_attempt_each_and_error_preserves_both_causes(audio, providers):
    _, primary, backup = providers
    backup.return_value = {"success": False, "transcript": "", "error": "fallback timeout"}
    result = stt.transcribe_audio(audio)
    assert result["success"] is False
    assert "HTTP 429" in result["error"]
    assert "fallback timeout" in result["error"]
    assert result["fallback_from"] == "elevenlabs"
    primary.assert_called_once()
    backup.assert_called_once()


def test_disabled_stt_never_sends_audio(audio, providers):
    config, primary, backup = providers
    config["enabled"] = False
    assert stt.transcribe_audio(audio)["success"] is False
    primary.assert_not_called()
    backup.assert_not_called()


def test_invalid_audio_never_sends_audio(providers):
    _, primary, backup = providers
    assert stt.transcribe_audio("/nonexistent/voice.wav")["success"] is False
    primary.assert_not_called()
    backup.assert_not_called()


def test_localized_fallback_notice_does_not_change_transcript(audio, providers):
    config, _, backup = providers
    config["fallback_notice"] = "⚠️ Распознавание: ElevenLabs → OpenAI (резерв)."
    result = stt.transcribe_audio(audio)
    assert result["fallback_notice"] == config["fallback_notice"]
    assert result["transcript"] == backup.return_value["transcript"]


def test_next_recording_retries_primary_not_sticky_fallback(audio, providers):
    _, primary, backup = providers
    assert stt.transcribe_audio(audio)["fallback_from"] == "elevenlabs"
    primary.return_value = {"success": True, "transcript": "Сервис восстановился.", "provider": "elevenlabs"}
    result = stt.transcribe_audio(audio)
    assert result["provider"] == "elevenlabs"
    assert "fallback_notice" not in result
    assert primary.call_count == 2
    backup.assert_called_once()
