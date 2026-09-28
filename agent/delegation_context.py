"""Execution-local Kanban ownership for in-process delegated agents.

Adapted from upstream 47bbc12 and a7dcf97 (trkim), and 474db53 (teknium1).
Never clear process-global worker identity: concurrent
children, the owner, and its heartbeat share os.environ.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Mapping

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_delegated_child_context", default=False,
)
DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"
KANBAN_ENV_KEYS = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
)


@contextmanager
def delegated_child_context() -> Iterator[None]:
    """Mark child construction/execution without changing the owner's environment."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    return _DELEGATED_CHILD_CONTEXT.get()


def is_delegated_child_process_context() -> bool:
    return is_delegated_child_context() or bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER))


def owned_kanban_task() -> str:
    """An inherited task id is not ownership; only the dispatcher worker owns it."""
    if is_delegated_child_process_context():
        return ""
    return (os.environ.get("HERMES_KANBAN_TASK") or "").strip()


def scrub_kanban_env(env: Mapping[str, str]) -> dict[str, str]:
    """Remove identity but retain board routing and a write fence across exec.

    A path-valued fence permits scratch-board reproductions in a separate home;
    it must never be re-derived after a descendant changes HERMES_HOME. This is
    cooperative runtime scoping, not OS confinement against arbitrary SQLite.
    """
    from hermes_cli.kanban_db import kanban_home

    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    cleaned[DELEGATED_CHILD_ENV_MARKER] = (
        env.get(DELEGATED_CHILD_ENV_MARKER)
        or os.environ.get(DELEGATED_CHILD_ENV_MARKER)
        or str(kanban_home())
    )
    return cleaned


def delegated_child_subprocess_env(env: Mapping[str, str]) -> dict[str, str]:
    """Bridge the context-local denial into a delegated child's subprocesses."""
    if is_delegated_child_process_context() or env.get(DELEGATED_CHILD_ENV_MARKER):
        return scrub_kanban_env(env)
    return dict(env)


def kanban_path_is_fenced(path: os.PathLike[str] | str) -> bool:
    """Deny in-process child writes, or descendant writes to its lineage's board."""
    if is_delegated_child_context():
        return True
    marker = os.environ.get(DELEGATED_CHILD_ENV_MARKER, "")
    if not marker:
        return False
    if marker == "1":
        return True
    target = Path(path).expanduser().resolve()
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned and target == Path(pinned).expanduser().resolve():
        return True
    return target.is_relative_to(Path(marker).expanduser().resolve())
