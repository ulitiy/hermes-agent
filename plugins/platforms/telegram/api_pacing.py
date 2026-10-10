"""Per-chat pacing shared by Telegram text, rich and control sends."""
import asyncio
from datetime import timedelta
import math
import re
import time

from gateway.platforms._shared import get_scoped_secret
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id


class TelegramApiPacingMixin:
    _bot_api_min_interval_seconds = 0.0

    def _configured_bot_api_min_interval(self):
        default = (getattr(self.config, "extra", {}) or {}).get("api_min_interval_seconds", 0.0)
        raw = get_scoped_secret("HERMES_TELEGRAM_API_MIN_INTERVAL_SECONDS") or default
        try:
            value = float(raw)
        except (ValueError, TypeError):
            value = 0.0
        return min(10.0, max(0.0, value)) if math.isfinite(value) else 0.0

    @staticmethod
    def _telegram_api_retry_after_seconds(exc):
        raw = getattr(exc, "retry_after", None)
        if isinstance(raw, timedelta):
            raw = raw.total_seconds()
        if raw is None:
            match = re.search(r"retry\s+(?:in|after)\s+([0-9]+(?:\.[0-9]+)?)", str(exc), re.I)
            raw = match.group(1) if match else None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, value) if math.isfinite(value) else None

    async def _rate_limited_telegram_call(self, chat_id, func, /, *args, **kwargs):
        interval = self._bot_api_min_interval_seconds
        if interval <= 0:
            return await func(*args, **kwargs)
        key = str(normalize_telegram_chat_id(chat_id))
        lock = self._bot_api_locks.setdefault(key, asyncio.Lock())
        async with lock:
            from telegram.error import RetryAfter
            from plugins.platforms.telegram.adapter import _FLOOD_INLINE_WAIT_CAP_SECS
            penalties = self.__dict__.setdefault("_bot_api_retry_after_until", {})
            remaining = penalties.get(key, 0.0) - time.monotonic()
            if remaining > _FLOOD_INLINE_WAIT_CAP_SECS:
                # No request has started: preserve the known flood refusal so
                # the send path/ledger can defer, never call this a timeout.
                raise RetryAfter(math.ceil(remaining))
            if remaining <= 0:
                penalties.pop(key, None)
            delay = self._bot_api_next_allowed_at.get(key, 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                wait = self._telegram_api_retry_after_seconds(exc)
                if wait is not None:
                    self._bot_api_next_allowed_at[key] = time.monotonic() + wait + 0.1
                    penalties[key] = self._bot_api_next_allowed_at[key]
                raise
            self._bot_api_next_allowed_at[key] = time.monotonic() + interval
            return result

    async def _paced_call_with_deadline(
        self, chat_id, func, /, *args, timeout, label, dump_on_blocked_loop=False, **kwargs,
    ):
        from plugins.platforms.telegram.adapter import _await_with_thread_deadline

        async def request():
            # Construct the coroutine only after the local queue/cooldown; the
            # watchdog measures transport time, not a known-unsent wait.
            return await _await_with_thread_deadline(
                func(*args, **kwargs), timeout=timeout, label=label,
                dump_on_blocked_loop=dump_on_blocked_loop,
            )

        return await self._rate_limited_telegram_call(chat_id, request)

    async def _paced_rich_edit(self, chat_id, payload):
        from plugins.platforms.telegram.adapter import _TEXT_SEND_DEADLINE
        try:
            return await self._paced_call_with_deadline(chat_id, self._bot.do_api_request, "editMessageText", api_kwargs=payload,
                                                        timeout=_TEXT_SEND_DEADLINE, label="telegram-send")
        except Exception as exc:
            wait = self._telegram_api_retry_after_seconds(exc)
            if wait is None or wait > 30:
                raise
            await asyncio.sleep(wait)
            return await self._paced_call_with_deadline(chat_id, self._bot.do_api_request, "editMessageText", api_kwargs=payload,
                                                        timeout=_TEXT_SEND_DEADLINE, label="telegram-send")
