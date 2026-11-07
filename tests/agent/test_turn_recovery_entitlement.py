"""A paid-entitlement recovery must freshly verify access before refreshing credentials."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.turn_recovery_entitlement import try_refresh_nous_paid_entitlement_credentials


@pytest.mark.parametrize("access", [True, False, None])
def test_refresh_requires_confirmed_paid_access(monkeypatch, access):
    lookup = Mock(return_value=SimpleNamespace(paid_service_access=access))
    monkeypatch.setattr("hermes_cli.nous_account.get_nous_portal_account_info", lookup)
    refresh = Mock(return_value=True)
    agent = SimpleNamespace(_try_refresh_nous_client_credentials=refresh)

    assert try_refresh_nous_paid_entitlement_credentials(agent) is (access is True)

    lookup.assert_called_once_with(force_fresh=True)
    if access is True:
        refresh.assert_called_once_with(force=True)
    else:
        refresh.assert_not_called()


def test_failed_entitlement_lookup_does_not_refresh(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.nous_account.get_nous_portal_account_info",
        Mock(side_effect=RuntimeError("account unavailable")),
    )
    refresh = Mock()
    agent = SimpleNamespace(_try_refresh_nous_client_credentials=refresh)

    assert try_refresh_nous_paid_entitlement_credentials(agent) is False
    refresh.assert_not_called()


@pytest.mark.parametrize("refresh", [Mock(return_value=False), Mock(side_effect=RuntimeError("refresh failed"))])
def test_failed_credential_refresh_is_not_recovered(monkeypatch, refresh):
    monkeypatch.setattr(
        "hermes_cli.nous_account.get_nous_portal_account_info",
        Mock(return_value=SimpleNamespace(paid_service_access=True)),
    )
    agent = SimpleNamespace(_try_refresh_nous_client_credentials=refresh)

    assert try_refresh_nous_paid_entitlement_credentials(agent) is False