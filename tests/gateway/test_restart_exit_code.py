"""Tests for the start_gateway exit-code logic after /restart.

The exit-decision block in ``start_gateway`` (gateway/run.py) chooses the
process exit code based on three flags:

  _signal_initiated_shutdown | _restart_requested | _restart_via_service
  ──────────────────────────┼────────────────────┼─────────────────────
  False                     │ True               │ False  → exit 1
  False                     │ True               │ True   → exit 75
  True                      │ False              │ -      → exit 1
  False                     │ False              │ False  → exit 0

The first row was missing before the fix — a /restart on launchd exited 0,
and ``KeepAlive.SuccessfulExit=false`` kept the gateway dead.
"""

from types import SimpleNamespace

import pytest

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE, _resolve_gateway_exit_verdict


@pytest.mark.parametrize("restart,signal,detached,expected", [
    (True, False, False, False), (True, True, False, False),
    (False, True, False, False), (False, False, False, True),
    (True, False, True, True),
])
def test_exit_verdict_preserves_revival_without_double_spawn(restart, signal, detached, expected):
    runner = SimpleNamespace(
        should_exit_with_failure=False, exit_code=None, _restart_via_service=False,
        _restart_requested=restart, _restart_detached=detached,
    )
    assert _resolve_gateway_exit_verdict(runner, signal) is expected


def test_service_restart_uses_reserved_exit_code():
    runner = SimpleNamespace(
        should_exit_with_failure=False, exit_code=None,
        _restart_requested=True, _restart_via_service=True,
    )
    with pytest.raises(SystemExit) as exc:
        _resolve_gateway_exit_verdict(runner, False)
    assert exc.value.code == GATEWAY_SERVICE_RESTART_EXIT_CODE
