"""The Desktop ownership gate survives multiplexing until tick's locked admission."""
import threading

import pytest


@pytest.mark.parametrize("drain_gate", [None, True, False])
def test_profile_gate_is_rechecked_by_each_locked_tick(tmp_path, monkeypatch, drain_gate):
    from cron import scheduler, jobs
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_constants import get_hermes_home

    homes = [("alpha", tmp_path / "alpha"), ("beta", tmp_path / "beta")]
    for _, home in homes:
        home.mkdir()
    stop = threading.Event()
    owner = {"alpha": False, "beta": False}
    draining = {"value": False}
    seen = []
    saved_gates = []

    def profile_gate(name, home):
        assert home == dict(homes)[name]
        return not owner[name]

    def tick(**kwargs):
        name = get_hermes_home().name
        gate = kwargs["can_dispatch"]
        saved_gates.append(gate)
        # The gateway starts between profile enumeration and locked job admission.
        owner[name] = True
        seen.append(gate() if gate is not None else True)
        if name == "beta":
            stop.set()
        return 0

    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **kw: None)
    monkeypatch.setattr(jobs, "clear_ticker_error", lambda: None)
    dispatch = None if drain_gate is None else lambda: not draining["value"]
    provider = InProcessCronScheduler()
    provider.start(stop, interval=0, profile_homes=homes, profile_gate=profile_gate,
                   can_dispatch=dispatch)

    assert seen == [False, False]
    # Captured callables must remain bound to their own profile, not the final loop item.
    owner["alpha"] = False
    assert saved_gates[0]() is True
    assert saved_gates[1]() is False
    owner["beta"] = False
    if drain_gate is not None:
        draining["value"] = not drain_gate
        assert all(gate() is drain_gate for gate in saved_gates)
