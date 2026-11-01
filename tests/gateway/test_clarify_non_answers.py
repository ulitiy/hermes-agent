"""Internal completions and status pings cannot answer a live human question."""
import pytest
from tools import clarify_gateway
from tests.gateway.test_busy_status_ping import _busy_ping


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['internal_completion', 'status_ping'])
async def test_non_answer_keeps_clarify_pending(kind):
    text = '[ASYNC DELEGATION BATCH COMPLETE] offline fixture' if kind == 'internal_completion' else 'Эй!'
    runner, adapter, agent, event, key, queued, _ = _busy_ping(mode='queue', text=text)
    event.internal = kind == 'internal_completion'
    entry = clarify_gateway.register('non-answer-fixture', key, 'Proceed?', ['Yes', 'No'])
    try:
        result = await runner._handle_message(event)
        assert clarify_gateway.get_pending_for_session(key, include_choice_prompts=True) is entry
        assert not entry.event.is_set()
        if kind == 'status_ping':
            assert isinstance(result, str) and 'Жду ответа' in result
            agent.interrupt.assert_not_called()
            agent.steer.assert_not_called()
            assert adapter._pending_messages == {key: queued}
    finally:
        clarify_gateway.clear_session(key)
