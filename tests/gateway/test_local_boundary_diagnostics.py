"""Observable best-effort boundaries must not print arbitrary private payloads."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["callback", "transcript", "model_metadata"])
async def test_gateway_warning_keeps_traceback_without_private_payload(boundary, caplog):
    private_payload = "unstructured-private-payload-not-a-recognizable-token"
    failure = RuntimeError(private_payload)
    caplog.set_level(logging.WARNING)
    if boundary == "callback":
        from gateway.platforms.session_progress import SessionProgressMixin
        first, second = Mock(side_effect=failure), AsyncMock()
        await SessionProgressMixin._chain_callbacks(first, second)()
        first.assert_called_once()
        second.assert_awaited_once()
    elif boundary == "transcript":
        from gateway.run_voice import GatewayVoiceMixin
        from gateway.config import Platform
        from gateway.session import SessionSource
        runner = Mock(spec=GatewayVoiceMixin)
        runner._format_stt_transcription_echo = lambda lines: "quoted"
        adapter = SimpleNamespace(send=AsyncMock(side_effect=failure))
        result = await GatewayVoiceMixin._echo_stt_transcripts(
            runner, adapter, SessionSource(platform=Platform.TELEGRAM, chat_id="fixture"), ["transcript"])
        assert result is False  # a failed echo must remain retryable
    else:
        from gateway.run_agent_cache import GatewayAgentCacheMixin
        db = SimpleNamespace(get_session=Mock(side_effect=failure))
        runner = Mock(spec=GatewayAgentCacheMixin)
        runner._session_db = SimpleNamespace(_db=db)
        GatewayAgentCacheMixin._sync_session_model_from_agent(
            runner, "fixture", SimpleNamespace(model="test-model"))
        db.get_session.assert_called_once_with("fixture")
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert "Traceback" in caplog.text and "RuntimeError" in caplog.text
    assert private_payload not in caplog.text
