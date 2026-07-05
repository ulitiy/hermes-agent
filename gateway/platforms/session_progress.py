"""Recover orphaned/pre-dispatch guards without interrupting real agent turns."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class SessionProgressMixin:
    def _ensure_session_progress_tracking(self):
        self.__dict__.setdefault("_session_guard_started_at", {})
        self.__dict__.setdefault("_session_handler_entered_at", {})
        self.__dict__.setdefault("_ownerless_session_guard_grace_seconds", 5.0)
        self.__dict__.setdefault("_pre_handler_guard_grace_seconds", 10.0)

    def _record_session_guard(self, session_key):
        self._ensure_session_progress_tracking()
        self._session_guard_started_at[session_key] = time.monotonic()
        self._session_handler_entered_at.pop(session_key, None)

    def mark_session_gateway_handler_entered(self, session_key):
        self._ensure_session_progress_tracking()
        if session_key in self._active_sessions:
            self._session_handler_entered_at[session_key] = time.monotonic()

    def _session_lock_stale_reason(self, session_key):
        self._ensure_session_progress_tracking()
        if session_key not in self._active_sessions:
            self._session_guard_started_at.pop(session_key, None)
            self._session_handler_entered_at.pop(session_key, None)
            return None
        now = time.monotonic()
        age = max(0, now - self._session_guard_started_at.setdefault(session_key, now))
        task = self._session_tasks.get(session_key)
        if task is None:
            return "ownerless_guard" if age >= self._ownerless_session_guard_grace_seconds else None
        if task.done():
            return "owner_task_done"
        if session_key not in self._session_handler_entered_at and age >= self._pre_handler_guard_grace_seconds:
            return "pre_handler_task_stalled"
        return None

    def _log_processing_task_exception(self, session_key, task):
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("Background task for %s failed before cleanup: %s", session_key, exc,
                         exc_info=(type(exc), exc, exc.__traceback__))
