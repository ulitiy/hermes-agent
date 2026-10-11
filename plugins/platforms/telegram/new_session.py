"""One-tap New uses the normal scoped command handler, never an unscoped reset."""
import logging

from gateway.platforms.base import MessageEvent, MessageType, SendResult
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger("plugins.platforms.telegram.adapter")


class TelegramNewSessionMixin:
    async def send_new_session_button(self, chat_id, *, text="Готово. Новая тема?", button_label="New", metadata=None):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if not self._bot:
            return SendResult(success=False, error="Not connected")
        reply_to = self._reply_to_message_id_for_send(None, metadata, reply_to_mode=self._reply_to_mode)
        try:
            msg = await self._send_message_with_thread_fallback(
                chat_id=normalize_telegram_chat_id(chat_id), text=text or "Готово. Новая тема?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(button_label or "New", callback_data="ns:new")]]),
                reply_to_message_id=reply_to,
                **self._thread_kwargs_for_send(
                    chat_id, self._metadata_thread_id(metadata), metadata,
                    reply_to_message_id=reply_to, reply_to_mode=self._reply_to_mode,
                ),
                **self._link_preview_kwargs(),
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as exc:
            from plugins.platforms.telegram.adapter import _redact_telegram_error_text
            error = _redact_telegram_error_text(exc)
            logger.warning("[%s] send_new_session_button failed: %s", self.name, error,
                           exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            return SendResult(success=False, error=error)

    def _source_from_callback_query(self, query):
        message, caller = query.message, query.from_user
        chat = message.chat
        raw_type = getattr(chat.type, "value", chat.type)
        chat_type = {"private": "dm", "supergroup": "group"}.get(raw_type, raw_type)
        thread_id = getattr(message, "message_thread_id", None)
        if raw_type == "supergroup" and thread_id is not None and (
            getattr(message, "is_topic_message", False) is True or getattr(chat, "is_forum", False) is True
        ):
            chat_type = "forum"
        return self.build_source(
            chat_id=str(message.chat_id), chat_type=chat_type,
            user_id=str(caller.id),
            user_name=getattr(caller, "username", None) or getattr(caller, "full_name", None) or getattr(caller, "first_name", None),
            thread_id=str(thread_id) if thread_id is not None and chat_type in {"forum", "dm"} else None,
        )

    async def _handle_new_session_callback(self, query, data, cb):
        if data != "ns:new" or not await self._callback_authorized(query, cb, "⛔ You are not authorized to answer this prompt."):
            return
        handler = getattr(self, "_message_handler", None)
        if not callable(handler) or query.message is None:
            await query.answer(text="Reset unavailable.", show_alert=True)
            return
        event = MessageEvent(
            text="/new", message_type=MessageType.COMMAND,
            source=self._source_from_callback_query(query), raw_message=query.message,
            message_id=str(query.message.message_id),
        )
        try:
            # Includes normal authorization, active-turn invalidation, profile scope and hooks.
            result = await handler(event)
            text, ttl = self._unwrap_ephemeral(result)
            await query.answer(text="✓ New session")
            await self._rate_limited_telegram_call(
                event.source.chat_id, self._edit_md_quiet, query, text or "✨ New session started!",
            )
            if ttl:
                self._schedule_ephemeral_delete(
                    chat_id=event.source.chat_id, message_id=event.message_id, ttl_seconds=ttl,
                )
        except Exception:
            logger.exception("[%s] new-session callback failed", self.name)
            await query.answer(text="❌ New session failed.", show_alert=True)
