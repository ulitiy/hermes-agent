"""Regression tests for #4707 — cron must be per-profile.

Design intent (Teknium, June 2026): a profile's cron jobs both LIVE in that
profile's HERMES_HOME and EXECUTE under it.

- Storage: a job created under profile ``coder`` writes to
  ``~/.hermes/profiles/coder/cron/jobs.json`` — NOT the shared default root.
- Execution: the profile-scoped gateway's in-process ticker resolves the
  active HERMES_HOME (profile home) at call time, so jobs run with that
  profile's ``.env`` / ``config.yaml`` / scripts / skills.

This is the opposite direction from the (reverted) #50112/#32091 "anchor at the
shared root" approach. Anchoring at the root funnels every profile's jobs into
one store and runs them under whatever HERMES_HOME the ticker happens to have —
leaking config/credentials/skills across profiles, the security boundary #4707
was filed for. These tests pin per-profile isolation so a stale-branch merge or
a re-anchor "fix" can't silently flip it back.
"""
import importlib
import os
from pathlib import Path


def _set_profile_env(monkeypatch, root: Path, profile_home: Path) -> None:
    """Pretend the platform default root is ``root`` and the active
    HERMES_HOME is a profile under it (``<root>/profiles/<name>``)."""
    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: root
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))


def test_cron_storage_anchors_at_profile_home(tmp_path, monkeypatch):
    """Under a profile HERMES_HOME (<root>/profiles/<name>), the cron store
    resolves to <profile>/cron, NOT the shared <root>/cron."""
    sandbox_home = os.environ["HERMES_HOME"]
    root = tmp_path / "hermes_home"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    import hermes_constants
    from cron import jobs
    try:
        with monkeypatch.context() as profile_patch:
            _set_profile_env(profile_patch, root, profile_home)
            assert hermes_constants.get_hermes_home().resolve() == profile_home.resolve()
            assert hermes_constants.get_default_hermes_root().resolve() == root.resolve()
            importlib.reload(jobs)
            assert jobs.HERMES_DIR.resolve() == profile_home.resolve()
            assert jobs.JOBS_FILE.resolve() == (profile_home / "cron" / "jobs.json").resolve()
            assert jobs.JOBS_FILE.resolve() != (root / "cron" / "jobs.json").resolve()
    finally:
        importlib.reload(jobs)
    assert os.environ["HERMES_HOME"] == sandbox_home


