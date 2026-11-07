"""Profile reset is a user-admission boundary, before durable FIFO ownership (GW-R2)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_authority import initialize_session_authority
from gateway.session_ingress import executing_admission
from hermes_state_runtime import claim_session_input, list_session_admissions, settle_session_input


@pytest.fixture(autouse=True)
def _profile_scope(tmp_path):
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(tmp_path, {}):
        yield


async def _gateway(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(
        'session_reset:\n  mode: none\nreset_by_profile:\n  task-manager:\n'
        f'    mode: {mode}\n    idle_minutes: 30\n    at_hour: 4\n')
    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace()
    monkeypatch.setattr(runner, '_adapter_for_source', lambda source: adapter)
    monkeypatch.setattr(runner, '_is_user_authorized_for_source', lambda *a, **kw: True)
    monkeypatch.setattr(runner, '_adapter_flag', lambda *a, **kw: False)
    monkeypatch.setattr(runner, '_is_telegram_topic_lane', lambda source: True)
    monkeypatch.setattr(runner, '_recover_telegram_lobby_thread_id', lambda source: None)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='reset-test')
    # Exercise the real commit, but stop before model/transport execution.
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42',
                           thread_id='7', chat_type='dm', behavior_profile='task-manager')
    first = runner.session_store.get_or_create_session(source)
    authority.register(source)
    assert runner._load_new_session_policy(source).mode == mode
    first.updated_at = datetime.now() - timedelta(days=2)
    first.last_prompt_tokens = 123
    runner.session_store._save()
    runner._record_telegram_topic_binding(source, first)
    return runner, authority, source, first


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['idle', 'daily', 'both', 'always'])
async def test_reset_precedes_fifo_owner_and_is_not_repeated_at_execution(tmp_path, monkeypatch, mode):
    runner, authority, source, first = await _gateway(tmp_path, monkeypatch, mode)
    receipt = await authority.admit_native(MessageEvent(text='new topic', source=source, message_id='m1'))
    current = runner.session_store.lookup_by_session_key(first.session_key)
    assert current is not None
    assert current.session_id != first.session_id
    assert receipt.ref.session_id == current.session_id
    assert current.was_auto_reset and current.reset_had_activity
    assert authority.db.get_session(first.session_id)['end_reason'] == ('idle' if mode == 'both' else mode)
    row = claim_session_input(authority.db, epoch=authority.epoch, session_id=current.session_id)
    assert isinstance(row, dict)
    event = MessageEvent(text='new topic', source=source, message_id='m1')
    event._claimed_owner = (receipt.ref.session_id, row['generation'])
    token = executing_admission.set(True)
    try:
        resolved = await runner._hmwa_resolve_session(event, source)
    finally:
        executing_admission.reset(token)
    assert resolved[1].session_id == receipt.ref.session_id  # especially mode=always
    binding = authority.db.get_telegram_topic_binding(
        chat_id='42', thread_id='7', profile_name=runner._telegram_topic_profile_name(source))
    assert binding['session_id'] == current.session_id
    assert len(list_session_admissions(authority.db, session_id=current.session_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['resume', 'active', 'process', 'queued', 'started', 'unknown', 'internal'])
async def test_policy_never_rotates_protected_work(tmp_path, monkeypatch, guard):
    runner, authority, source, first = await _gateway(tmp_path, monkeypatch, 'always')
    setup = {
        'resume': lambda: setattr(first, 'resume_pending', True),
        'active': lambda: setattr(first, 'active_turn_token', 'running-turn'),
        'process': lambda: monkeypatch.setattr(runner.session_store, '_has_active_processes_safe', lambda *a, **kw: True),
    }.get(guard)
    if setup:
        setup()
    if guard in {'queued', 'started', 'unknown'}:
        from hermes_state_runtime import admit_session_input, recover_session_inputs, begin_runtime_epoch
        admit_session_input(authority.db, epoch=authority.epoch, principal_id='other',
                            session_id=first.session_id, request_id='earlier', payload={'text': 'work'})
        if guard != 'queued':
            claim_session_input(authority.db, epoch=authority.epoch, session_id=first.session_id)
        if guard == 'unknown':
            authority.epoch = begin_runtime_epoch(authority.db, instance_id='replacement')
            recover_session_inputs(authority.db, epoch=authority.epoch)
    runner.session_store._save()
    if guard == 'internal':
        before = first.updated_at
        resolved = await runner._hmwa_resolve_session(MessageEvent(text='wake', source=source, internal=True), source)
        assert resolved[1].session_id == first.session_id
        assert first.updated_at == before
    else:
        receipt = await authority.admit_native(MessageEvent(text='follow up', source=source, message_id='m2'))
        assert receipt.ref.session_id == first.session_id
    assert runner.session_store.peek_session_id(first.session_key) == first.session_id


@pytest.mark.asyncio
async def test_terminal_redelivery_does_not_reset_or_duplicate_always_policy(tmp_path, monkeypatch):
    runner, authority, source, first = await _gateway(tmp_path, monkeypatch, 'always')
    event = MessageEvent(text='once', source=source, message_id='same')
    receipt = await authority.admit_native(event)
    row = claim_session_input(authority.db, epoch=authority.epoch, session_id=receipt.ref.session_id)
    assert isinstance(row, dict)
    settle_session_input(authority.db, epoch=authority.epoch, admission_id=row['admission_id'],
                         generation=row['generation'], outcome='completed')
    replay = await authority.admit_native(MessageEvent(
        text='once', source=source, message_id='same', timestamp=event.timestamp))
    assert replay.admission_id == receipt.admission_id
    assert replay.ref == receipt.ref
    assert runner.session_store.peek_session_id(first.session_key) == receipt.ref.session_id
    next_receipt = await authority.admit_native(MessageEvent(text='next', source=source, message_id='next'))
    assert next_receipt.ref.session_id != receipt.ref.session_id


@pytest.mark.asyncio
async def test_admission_during_compression_does_not_wait_or_reset(tmp_path, monkeypatch):
    import asyncio
    _runner, authority, source, first = await _gateway(tmp_path, monkeypatch, 'always')
    # Compression deliberately holds the claim gate, not admissions, across its LLM call.
    async with authority.sessions[first.session_id].claim_gate:
        receipt = await asyncio.wait_for(authority.admit_native(
            MessageEvent(text='while compressing', source=source, message_id='m1')), timeout=3)
    assert receipt.ref.session_id == first.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['none', 'idle', 'daily', 'both'])
async def test_unexpired_policy_keeps_fifo_owner(tmp_path, monkeypatch, mode):
    runner, authority, source, first = await _gateway(tmp_path, monkeypatch, mode)
    first.updated_at = datetime.now()
    runner.session_store._save()
    receipt = await authority.admit_native(MessageEvent(text='same topic', source=source, message_id='m1'))
    assert receipt.ref.session_id == first.session_id
    assert not first.was_auto_reset
