"""Local Telegram callbacks, separated from the transport adapter."""

import asyncio
import logging
import subprocess
import sys

from hermes_constants import get_hermes_home
from tools.environments.local import served_profile_child_env

logger = logging.getLogger(__name__)


class TelegramLocalCallbacksMixin:
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
