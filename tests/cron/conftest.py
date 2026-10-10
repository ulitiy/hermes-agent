"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_cron_store(_hermetic_environment, monkeypatch):
    """Rebind import-time defaults per test, without reloading shared modules.

    Individual fixtures may still override these defaults for profile tests.
    Patching constants (not use_cron_store) also covers spawned test threads.
    """
    import cron.jobs as jobs

    home = Path(os.environ["HERMES_HOME"])
    cron = home / "cron"
    for name, path in {
        "HERMES_DIR": home,
        "CRON_DIR": cron,
        "JOBS_FILE": cron / "jobs.json",
        "OUTPUT_DIR": cron / "output",
        "TICKER_HEARTBEAT_FILE": cron / "ticker_heartbeat",
        "TICKER_SUCCESS_FILE": cron / "ticker_last_success",
    }.items():
        monkeypatch.setattr(jobs, name, path)


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield
