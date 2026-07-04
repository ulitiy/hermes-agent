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
            delay = self._bot_api_next_allowed_at.get(key, 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                wait = self._telegram_api_retry_after_seconds(exc)
                if wait is not None:
                    self._bot_api_next_allowed_at[key] = time.monotonic() + wait + 0.1
                raise
            self._bot_api_next_allowed_at[key] = time.monotonic() + interval
            return result

    async def _paced_rich_edit(self, chat_id, payload):
        try:
            return await self._rate_limited_telegram_call(chat_id, self._bot.do_api_request, "editMessageText", api_kwargs=payload)
        except Exception as exc:
            wait = self._telegram_api_retry_after_seconds(exc)
            if wait is None or wait > 30:
                raise
            await asyncio.sleep(wait)
            return await self._rate_limited_telegram_call(chat_id, self._bot.do_api_request, "editMessageText", api_kwargs=payload)
