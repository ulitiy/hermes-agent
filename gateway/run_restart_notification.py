"""Durable chat /restart notices, preserving upstream profile/relay transport routing."""
from __future__ import annotations

import asyncio
import json
import logging

from gateway.config import Platform
from gateway.run_shutdown import _send_error, _send_failed
from agent.i18n import t

logger = logging.getLogger("gateway.run")


class GatewayRestartNotificationMixin:
    def _schedule_restart_notification_watch(self) -> None:
        existing = getattr(self, "_restart_notification_task", None)
        if existing is not None and not existing.done():
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._watch_restart_notification())
        self._restart_notification_task = task
        self._background_tasks.add(task)

        def finished(task):
            self._background_tasks.discard(task)
            if getattr(self, "_restart_notification_task", None) is task:
                self._restart_notification_task = None

        task.add_done_callback(finished)

    async def _watch_restart_notification(
        self, *, initial_delay: float = 1.0, max_delay: float = 8.0, timeout: float = 45.0,
    ) -> None:
        """Bound retries in this process; keep the marker for later recovery on timeout."""
        from gateway.run import _restart_notification_pending

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        delay, max_delay = max(0.0, initial_delay), max(0.0, max_delay)
        while _restart_notification_pending() and loop.time() < deadline:
            if not getattr(self, "_running", False) or getattr(self, "_draining", False):
                return
            await asyncio.sleep(min(delay, max(0.0, deadline - loop.time())))
            if not getattr(self, "_running", False) or getattr(self, "_draining", False):
                return
            if not _restart_notification_pending():
                return
            delivered = await self._send_restart_notification()
            if not _restart_notification_pending():
                return
            # Drain a newer pending marker after a recovered claimed marker.
            delay = 0.0 if delivered is not None else min(max_delay, delay * 2) if delay > 0 else max_delay
        if _restart_notification_pending():
            logger.warning("Restart notification retry timed out after %.1fs; marker retained for the next gateway startup", timeout)

    async def _send_restart_notification(self) -> tuple[str, str, str | None] | None:
        """Atomically claim a marker without overwriting a newer pending notice."""
        from gateway.delivery import resolve_delivery_transport
        from gateway.run import _hermes_home, _non_conversational_metadata

        notify_path = _hermes_home / ".restart_notify.json"
        claimed_path = _hermes_home / ".restart_notify.claimed.json"
        if not notify_path.exists() and not claimed_path.exists():
            return None
        cleanup = True
        try:
            if not claimed_path.exists():
                try:
                    notify_path.replace(claimed_path)
                except FileNotFoundError:
                    if not claimed_path.exists():
                        return None
            data = json.loads(claimed_path.read_text(encoding="utf-8-sig"))
            platform_str, chat_id, thread_id = (data.get(k) for k in ("platform", "chat_id", "thread_id"))
            if not platform_str or not chat_id:
                return None
            platform = Platform(platform_str)
            transport = resolve_delivery_transport(
                platform, self.config, self._adapters_for_profile(self._marker_profile(data)))
            if transport is None:
                logger.debug("Restart notification skipped: no live transport for %s", platform_str)
                return None
            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info("Restart notification suppressed: %s has gateway_restart_notification=false", platform_str)
                return None
            metadata = self._pending_marker_metadata(platform, chat_id, data, transport.adapter)
            if data.get("delivered_via_upstream_relay") is True:
                metadata = dict(metadata or {})
                for field in ("user_id", "scope_id"):
                    if data.get(field):
                        metadata[field] = str(data[field])
            result = await transport.send(
                platform, str(chat_id), t("gateway.startup.restarted"),
                metadata=_non_conversational_metadata(metadata, platform=platform),
            )
            if _send_failed(result):
                cleanup = not bool(getattr(result, "retryable", False))
                logger.log(
                    logging.WARNING if cleanup else logging.INFO,
                    "Restart notification to %s:%s was not delivered%s: %s",
                    platform_str, chat_id, "" if cleanup else " (deferred for retry)", _send_error(result),
                )
                return None
            logger.info("Sent restart notification to %s:%s", platform_str, chat_id)
            return str(platform_str), str(chat_id), str(thread_id) if thread_id else None
        except asyncio.CancelledError:
            cleanup = False
            raise
        except Exception as exc:
            logger.warning("Restart notification failed: %s", exc)
            return None
        finally:
            if cleanup:
                claimed_path.unlink(missing_ok=True)
