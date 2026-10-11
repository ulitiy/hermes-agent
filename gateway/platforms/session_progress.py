"""Session progress, recovery of orphaned guards, and post-delivery lifecycle."""
import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _split_post_delivery_entry(entry: Any) -> tuple[Optional[int], Any]:
    """``(generation, callback)`` from a slot; legacy callbacks have no generation."""
    return entry if isinstance(entry, tuple) and len(entry) == 2 else (None, entry)


class SessionProgressMixin:
    def register_post_delivery_callback(
        self, session_key: str, callback: Callable, *, generation: int | None = None) -> None:
        """Chain same-generation callbacks; a stale generation cannot replace a fresher slot."""
        if not session_key or not callable(callback):
            return
        existing = self._post_delivery_callbacks.get(session_key)
        if existing is not None:
            existing_gen, existing_cb = _split_post_delivery_entry(existing)
            if existing_gen is not None and generation is not None and int(generation) < int(existing_gen):
                return
            if callable(existing_cb) and (
                existing_gen is None or generation is None or int(existing_gen) == int(generation)):
                callback = self._chain_callbacks(existing_cb, callback)
        self._post_delivery_callbacks[session_key] = (
            callback if generation is None else (int(generation), callback))

    @staticmethod
    def _chain_callbacks(*callbacks: Callable) -> Callable[[], Awaitable[None]]:
        """Run callbacks in order with per-callback exception isolation, awaiting async hooks."""
        async def _chained() -> None:
            for callback in callbacks:
                try:
                    result = callback()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logger.warning("Post-delivery callback failed",
                                   exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
        return _chained

    def pop_post_delivery_callback(
        self, session_key: str, *, generation: int | None = None) -> Callable | None:
        """Pop a deferred callback, optionally requiring generation ownership."""
        entry = self._post_delivery_callbacks.get(session_key) if session_key else None
        if entry is None:
            return None
        entry_generation, callback = _split_post_delivery_entry(entry)
        if generation is not None and (entry_generation is None or int(entry_generation) != int(generation)):
            return None
        self._post_delivery_callbacks.pop(session_key, None)
        return callback if callable(callback) else None

    async def _consume_reaction_only_response(self, response, event, record_delivery) -> bool:
        """Consume an exact control marker even when the native reaction fails."""
        from gateway.response_filters import parse_reaction_only_response

        emoji = parse_reaction_only_response(response)
        if not emoji:
            return False
        event.metadata["_hermes_reaction_only_response"] = emoji
        logger.info("[%s] Sending reaction-only response %s to %s", self.name, emoji, event.source.chat_id)
        result = await self.send_reaction_only_response(event, emoji)
        record_delivery(result)
        if result is not None and not getattr(result, "success", False):
            logger.warning("[%s] Reaction-only response failed for %s: %s",
                           self.name, event.source.chat_id, getattr(result, "error", None))
        return True

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
