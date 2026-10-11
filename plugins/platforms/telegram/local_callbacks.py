"""Local Telegram callbacks, separated from the transport adapter."""

import asyncio
import logging
import subprocess
import sys
from datetime import datetime

from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import MessageEvent, MessageType
from hermes_constants import get_hermes_home
from tools.environments.local import served_profile_child_env

logger = logging.getLogger(__name__)


class TelegramLocalCallbacksMixin:
    async def _start_injection_server(self) -> None:
        """Authenticated loopback entry for local transcripts, scoped to this transport."""
        secret = (get_scoped_secret("HERMES_TELEGRAM_INJECT_SECRET") or "").strip()
        if not secret:
            self._injection_runner = None
            return
        if getattr(self, "_injection_runner", None) is not None:
            return
        try:
            from aiohttp import web
        except ImportError:
            logger.warning("[%s] aiohttp missing — injection endpoint disabled", self.name)
            self._injection_runner = None
            return
        self._injection_tasks = set()
        port = int(get_scoped_secret("HERMES_TELEGRAM_INJECT_PORT") or "8643")

        async def handler(request):
            if request.headers.get("X-Inject-Secret") != secret:
                return web.json_response({"error": "forbidden"}, status=403)
            if self._teardown_started:
                return web.json_response({"error": "disconnecting"}, status=503)
            try:
                data = await request.json()
            except (ValueError, UnicodeError):
                return web.json_response({"error": "bad_json"}, status=400)
            if not isinstance(data, dict):
                return web.json_response({"error": "bad_json"}, status=400)
            chat_id, text = data.get("chat_id"), data.get("text")
            if not chat_id or not text:
                return web.json_response({"error": "missing chat_id/text"}, status=400)
            thread_id = data.get("thread_id") or data.get("message_thread_id")
            try:
                event = self._build_synthetic_text_event(
                    chat_id=str(chat_id), text=str(text),
                    user_id=str(data.get("user_id") or chat_id),
                    user_name=str(data.get("user_name") or "User"),
                    thread_id=str(thread_id) if thread_id is not None else None,
                    chat_type=str(data.get("chat_type") or ("group" if thread_id is not None else "dm")),
                )
                task = asyncio.create_task(self.handle_message(event))
                self._injection_tasks.add(task)
                task.add_done_callback(self._injection_dispatch_done)
            except Exception as exc:
                logger.error("Injection dispatch failed",
                             exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
                return web.json_response({"error": "dispatch_failed"}, status=500)
            return web.json_response({"status": "queued"})

        app = web.Application()
        app.router.add_post("/inject", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._injection_runner = runner
        logger.info("[%s] Injection endpoint listening on 127.0.0.1:%d (POST /inject)", self.name, port)

    def _injection_dispatch_done(self, task) -> None:
        self._injection_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Injection dispatch task failed",
                         exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))

    async def _stop_injection_server(self) -> None:
        from plugins.platforms.telegram.adapter import _DISCONNECT_STEP_TIMEOUT

        runner = getattr(self, "_injection_runner", None)
        self._injection_runner = None
        try:
            if runner is not None:
                await self._await_disconnect_step(runner.cleanup(), _DISCONNECT_STEP_TIMEOUT, "injection cleanup")
        except Exception as exc:
            logger.warning("Injection endpoint cleanup failed",
                           exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
        finally:
            pending = self._collect_live_tasks(getattr(self, "_injection_tasks", ()), asyncio.current_task())
            for task in pending:
                task.cancel()
            if pending:
                await self._await_disconnect_step(
                    asyncio.gather(*pending, return_exceptions=True), _DISCONNECT_STEP_TIMEOUT, "injection dispatch cancel")

    def _build_synthetic_text_event(
        self, chat_id: str, text: str, user_id: str, user_name: str,
        thread_id: str | None = None, chat_type: str = "dm",
    ) -> MessageEvent:
        source = self.build_source(
            chat_id=chat_id, chat_name=user_name, chat_type=chat_type,
            user_id=user_id, user_name=user_name, thread_id=thread_id,
            chat_topic=None, message_id=None,
        )
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                            raw_message=None, message_id=None, timestamp=datetime.now())

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
