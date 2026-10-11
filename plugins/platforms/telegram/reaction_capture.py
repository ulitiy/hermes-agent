"""Telegram reaction lifecycle and opt-in capture on indexed bot replies."""

import logging

from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger(__name__)


class TelegramReactionCaptureMixin:
    def _reactions_enabled(self) -> bool:
        """Scoped env → profile YAML → off; a multiplex miss never reads another profile."""
        from plugins.platforms.telegram.adapter import _extra_or_secret

        configured = _extra_or_secret(self.config.extra, "reactions", "TELEGRAM_REACTIONS", None)
        if configured is None:
            return False
        return str(configured).lower() not in {"false", "0", "no"}

    async def _set_reaction(self, chat_id: str, message_id: str, emoji: str | None) -> bool:
        """Set a single emoji; None clears all bot-set reactions."""
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(chat_id=normalize_telegram_chat_id(chat_id), message_id=int(message_id), reaction=emoji)
            return True
        except Exception as exc:
            logger.exception("[%s] Telegram reaction failed (%s)", self.name, emoji,
                             exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            return False

    async def _clear_reactions(self, chat_id: str, message_id: str) -> bool:
        """Clear all bot-set reactions."""
        return await self._set_reaction(chat_id, message_id, None)

    async def send_reaction_only_response(self, event: MessageEvent, emoji: str) -> SendResult:
        """React to the inbound message without a text reply."""
        chat_id = getattr(getattr(event, "source", None), "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not (chat_id and message_id):
            return SendResult(success=False, error="reaction-only reply missing Telegram chat_id/message_id")
        ok = await self._set_reaction(chat_id, message_id, emoji)
        return SendResult(success=ok, message_id=str(message_id) if ok else None,
                          error=None if ok else "Telegram set_message_reaction failed")

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction when message processing begins."""
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._set_reaction(chat_id, message_id, "\U0001f440")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Replace progress with the outcome, except after an intentional reaction-only reply."""
        if getattr(event, "metadata", {}).get("_hermes_reaction_only_response"):
            return
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not (chat_id and message_id):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            await self._clear_reactions(chat_id, message_id)
        else:
            await self._set_reaction(chat_id, message_id, "\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\U0001f44e")

    @staticmethod
    def _reaction_capture_enabled() -> bool:
        return (get_scoped_secret("HERMES_TELEGRAM_REACTION_CAPTURE") or "false").lower() in {
            "1", "true", "yes", "on",
        }

    def _session_db(self):
        store = getattr(self, "_session_store", None)
        return getattr(store, "_db", None) if store is not None else None

    def _resolve_session_id_for_key(self, session_key):
        store = getattr(self, "_session_store", None)
        if store is None or not session_key:
            return None
        try:
            store._ensure_loaded()
            entry = store._entries.get(session_key)
            return getattr(entry, "session_id", None) if entry else None
        except Exception as exc:
            logger.warning("Reaction capture could not resolve session",
                           exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            return None

    def _on_final_response_sent(self, event, session_key, result, text_content) -> None:
        if not self._reaction_capture_enabled():
            return
        db = self._session_db()
        message_id = getattr(result, "message_id", None)
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", None)
        if db is None or not (getattr(result, "success", False) and message_id and chat_id):
            return
        thread_id = getattr(source, "thread_id", None)
        try:
            db.record_telegram_outbound_message(
                chat_id=str(chat_id), message_id=str(message_id),
                thread_id=str(thread_id) if thread_id is not None else None,
                session_id=self._resolve_session_id_for_key(session_key), snippet=text_content,
            )
        except Exception:
            logger.debug("Outbound reaction indexing failed", exc_info=True)

    @staticmethod
    def _diff_reactions(old_list, new_list) -> tuple:
        def _key(reaction):
            if emoji := getattr(reaction, "emoji", None):
                return "emoji", emoji
            if custom_id := getattr(reaction, "custom_emoji_id", None):
                return "custom_emoji", str(custom_id)
            if rtype := getattr(reaction, "type", None):
                return str(rtype), str(rtype)
            return None
        old_keys = [k for r in (old_list or []) if (k := _key(r))]
        new_keys = [k for r in (new_list or []) if (k := _key(r))]
        old_set, new_set = set(old_keys), set(new_keys)
        return ([k for k in new_keys if k not in old_set],
                [k for k in old_keys if k not in new_set])

    async def _handle_message_reaction(self, update, context) -> None:
        if not self._reaction_capture_enabled():
            return
        mr = getattr(update, "message_reaction", None)
        db = self._session_db()
        if mr is None or db is None:
            return
        try:
            chat_id = getattr(getattr(mr, "chat", None), "id", None)
            message_id = getattr(mr, "message_id", None)
            if chat_id is None or message_id is None:
                return
            indexed = db.lookup_telegram_outbound_message(chat_id=str(chat_id), message_id=str(message_id))
            if indexed is None:
                return
            actor = getattr(mr, "user", None)
            user_id = getattr(actor, "id", None)
            user_name = (getattr(actor, "username", None) or getattr(actor, "full_name", None)
                         or getattr(actor, "first_name", None))
            added, removed = self._diff_reactions(mr.old_reaction, mr.new_reaction)
            for action, reactions in (("add", added), ("remove", removed)):
                for kind, value in reactions:
                    db.record_telegram_reaction(
                        chat_id=str(chat_id), message_id=str(message_id), emoji=value,
                        reaction_kind=kind, action=action, thread_id=indexed.get("thread_id"),
                        user_id=str(user_id) if user_id is not None else None,
                        user_name=str(user_name) if user_name else None,
                        session_id=indexed.get("session_id"), snippet=indexed.get("snippet"),
                    )
        except Exception:
            logger.warning("Failed to handle Telegram message reaction", exc_info=True)
