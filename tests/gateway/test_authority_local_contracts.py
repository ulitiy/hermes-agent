"""Local reply contracts must survive the authority's no-waiter delivery path."""
from types import SimpleNamespace

import pytest

from gateway import session_ingress
from hermes_constants import get_hermes_home
from tests.gateway.test_reaction_only_reply import _ReactionOnlyAdapter, _make_event


def _authority(adapter, home):
    return SimpleNamespace(
        runner=SimpleNamespace(
            config=SimpleNamespace(multiplex_profiles=True),
            _adapter_for_source=lambda source: adapter,
        ),
        profile_id=str(home), native_waiters=set(), pending_deliveries={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [None, 7])
async def test_no_waiter_post_delivery_runs_once_in_owner_scope(tmp_path, monkeypatch, generation):
    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setattr("gateway.run._load_profile_secret_scope", lambda home: {})
    adapter = _ReactionOnlyAdapter()
    event = _make_event()
    authority = _authority(adapter, home)
    seen = []

    def callback():
        assert adapter.sent
        seen.append(get_hermes_home())

    adapter.register_post_delivery_callback("route", callback, generation=generation)
    session_ingress._take_post_delivery(authority, "admission", event.source, "route")
    assert "route" not in adapter._post_delivery_callbacks
    authority.pending_deliveries["admission"] = (adapter, event, "route", "Delivered answer", None)
    await session_ingress.deliver_settled(authority, "admission")
    await session_ingress.fire_post_delivery(authority, "admission")
    await session_ingress.fire_post_delivery(authority, "admission")
    assert seen == [home]
    assert get_hermes_home() != home


@pytest.mark.asyncio
async def test_late_native_waiter_gets_post_delivery_slot_back(tmp_path):
    adapter = _ReactionOnlyAdapter()
    event = _make_event()
    authority = _authority(adapter, tmp_path)
    seen = []
    callback = lambda: seen.append("called")
    adapter.register_post_delivery_callback("route", callback, generation=7)
    session_ingress._take_post_delivery(authority, "admission", event.source, "route")
    authority.native_waiters.add("admission")
    await session_ingress.fire_post_delivery(authority, "admission")
    assert seen == []
    assert adapter.pop_post_delivery_callback("route", generation=7) is callback
    assert not authority.post_delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction", ["success", "failure", "unsupported", "exception"])
async def test_settled_reaction_marker_never_becomes_text(tmp_path, monkeypatch, reaction):
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import ProcessingOutcome
    from gateway.session_producer_lifecycle import _Bracket

    adapter = _ReactionOnlyAdapter()
    if reaction == "unsupported":
        adapter.send_reaction_only_response = BasePlatformAdapter.send_reaction_only_response.__get__(adapter)
    elif reaction in {"failure", "exception"}:
        async def failed(event, emoji):
            adapter.reactions.append({"emoji": emoji})
            if reaction == "exception":
                raise RuntimeError("transport failed")
            return SendResult(success=False, error="reaction refused")
        adapter.send_reaction_only_response = failed
    event = _make_event()
    authority = _authority(adapter, tmp_path)
    monkeypatch.setattr("gateway.run._load_profile_secret_scope", lambda home: {})
    authority.producer_turns = {"admission": _Bracket(adapter, event, hooks=True)}
    authority.pending_deliveries["admission"] = (adapter, event, "route", "REACTION_ONLY: 👍", None)
    await session_ingress.deliver_settled(authority, "admission")
    await session_ingress.deliver_settled(authority, "admission")
    assert adapter.sent == []
    assert event.metadata["_hermes_reaction_only_response"] == "👍"
    assert len(adapter.reactions) == int(reaction != "unsupported")
    expected = ProcessingOutcome.SUCCESS if reaction == "success" else ProcessingOutcome.FAILURE
    assert adapter.completed == [expected]
    assert not authority.pending_deliveries


@pytest.mark.asyncio
async def test_marker_mentioned_in_prose_is_still_delivered():
    adapter = _ReactionOnlyAdapter()
    event = _make_event()
    text = "The marker REACTION_ONLY: 👍 is an example, not this reply."
    assert await session_ingress.deliver_response(adapter, event, "route", text)
    assert [sent["content"] for sent in adapter.sent] == [text]
    assert adapter.reactions == []
    assert "_hermes_reaction_only_response" not in event.metadata
