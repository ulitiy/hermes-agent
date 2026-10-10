"""An opted-in parent must not leak board offers to delegated descendants."""
from contextlib import nullcontext

import pytest


@pytest.mark.parametrize("child", ["context", "process"])
@pytest.mark.parametrize("surface", ["skills", "lifecycle", "orchestrator"])
def test_opted_in_profile_hides_child_offers(tmp_path, monkeypatch, child, surface):
    from agent.delegation_context import delegated_child_context, DELEGATED_CHILD_ENV_MARKER
    from agent.skill_utils import skill_matches_environment
    from tools import kanban_tools as tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    (tmp_path / "config.yaml").write_text("platform_toolsets:\n  cli: [kanban]\n")
    checks = {
        "skills": lambda: skill_matches_environment({"environments": ["kanban"]}),
        "lifecycle": tools._check_kanban_mode,
        "orchestrator": tools._check_kanban_orchestrator_mode,
    }
    check = checks[surface]
    assert check()
    with monkeypatch.context() as child_env:
        if child == "process":
            child_env.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
        with delegated_child_context() if child == "context" else nullcontext():
            assert not check()
    assert check()
