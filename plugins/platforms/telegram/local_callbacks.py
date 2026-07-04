"""Local Telegram callbacks, separated from the transport adapter."""

import asyncio
import logging
import subprocess
import sys

from hermes_constants import get_hermes_home
from tools.environments.local import served_profile_child_env

logger = logging.getLogger(__name__)


class TelegramLocalCallbacksMixin:
    async def _handle_plugin_callback_query(self, query, data, cb):
        from hermes_cli.plugins import has_hook, invoke_hook
        if not has_hook("telegram_callback_query"):
            return False
        caller = getattr(query, "from_user", None)
        message = getattr(query, "message", None)
        user_name = cb["user_name"] or getattr(caller, "username", None)
        try:
            results = await asyncio.to_thread(
                invoke_hook, "telegram_callback_query", callback_data=data,
                user_id=str(getattr(caller, "id", "") or ""),
                user_name=str(user_name).strip() if user_name else None,
                **{key: str(cb[key]) if cb[key] is not None else None for key in ("chat_id", "chat_type", "thread_id")},
                message_text=getattr(message, "text", None), adapter=self, raw_query=query,
            )
        except Exception:
            logger.exception("telegram_callback_query hook failed")
            return False
        for result in results:
            if not isinstance(result, dict):
                continue
            action = str(result.get("action") or "").strip().lower()
            if action == "allow" or not (result.get("handled") or action in {"handled", "answer", "skip"}):
                continue
            answer = result.get("answer_text")
            if answer is not False:
                if answer is None:
                    answer = result.get("text") if action == "answer" else "✓"
                try:
                    await query.answer(text=str(answer)[:200], show_alert=bool(result.get("show_alert", False)))
                except Exception:
                    logger.debug("Plugin callback answer failed", exc_info=True)
            edit = result.get("edit_text")
            if isinstance(edit, str):
                markup = None if result.get("remove_keyboard", True) else getattr(message, "reply_markup", None)
                try:
                    await query.edit_message_text(text=edit, parse_mode=result.get("parse_mode"), reply_markup=markup)
                except Exception:
                    logger.debug("Plugin callback edit failed", exc_info=True)
            return True
        return False

    async def _handle_ack_alert_callback(self, query, data, cb):
        if not await self._callback_authorized(
            query, cb, "⛔ You are not authorized to acknowledge this alert.",
        ):
            return
        alert_id = data.split(":", 1)[1].strip()
        caller_id = str(getattr(query.from_user, "id", ""))
        user_display = getattr(query.from_user, "first_name", None) or cb["user_name"] or "User"
        home = get_hermes_home()
        script = home / "scripts" / "ack_escalation_notify.py"
        if not script.is_file():
            await query.answer(text="❌ Ack script missing.")
            return
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(script), "ack", alert_id,
                 "--user", str(user_display), "--user-id", caller_id, "--json"],
                env=served_profile_child_env(target_home=home),
                text=True, capture_output=True, timeout=30, check=False,
            )
            if result.returncode == 0:
                await query.answer(text="✅ Подтверждено. Звонки остановлены.")
            else:
                detail = (result.stderr or result.stdout or "ack failed").strip()[:160]
                await query.answer(text=f"❌ Ack failed: {detail}")
                logger.error("Alert acknowledgement failed rc=%s", result.returncode)
        except Exception:
            await query.answer(text="❌ Ack failed.")
            logger.exception("Alert acknowledgement callback failed")
