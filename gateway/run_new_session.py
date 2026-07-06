"""Local opt-in session boundaries and Telegram's post-turn New affordance.

Upstream's old global reset policy stays inert. The local policy is passed only
at user-turn admission, never on internal wakes or post-turn bookkeeping reads.
Topic roles select policy without changing the transport's home or identity.
"""
from __future__ import annotations

import copy
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta

from gateway.config import Platform, _coerce_bool, _coerce_int


@dataclass(frozen=True)
class NewSessionPolicy:
    mode: str = "none"
    at_hour: int = 4
    idle_minutes: int = 1440
    notify: bool = True
    notify_exclude_platforms: tuple = ("api_server", "webhook")
    post_task_new_button: bool = False
    post_task_new_button_text: str = "Готово. Новая тема?"
    post_task_new_button_label: str = "New"

    @classmethod
    def from_config(cls, config: dict, profile: str | None = None):
        raw = config.get("session_reset")
        raw = raw if isinstance(raw, dict) else {}
        overrides = config.get("reset_by_profile")
        if not isinstance(overrides, dict):
            overrides = raw.get("by_profile") or raw.get("profiles") or raw.get("reset_by_profile") or {}
        selected = overrides.get(profile) if isinstance(overrides, dict) and profile else None
        selected = selected if isinstance(selected, dict) else raw
        mode = str(selected.get("mode") or "none").lower()
        if mode not in {"none", "always", "idle", "daily", "both"}:
            mode = "none"
        exclude = selected.get("notify_exclude_platforms")
        return cls(
            mode=mode,
            at_hour=min(23, max(0, _coerce_int(selected.get("at_hour"), 4))),
            idle_minutes=max(1, _coerce_int(selected.get("idle_minutes"), 1440)),
            notify=_coerce_bool(selected.get("notify"), True),
            notify_exclude_platforms=tuple(exclude) if isinstance(exclude, (tuple, list)) else cls.notify_exclude_platforms,
            post_task_new_button=_coerce_bool(selected.get("post_task_new_button"), False),
            post_task_new_button_text=str(selected.get("post_task_new_button_text") or cls.post_task_new_button_text),
            post_task_new_button_label=str(selected.get("post_task_new_button_label") or cls.post_task_new_button_label),
        )

    def reset_reason(self, updated_at: datetime, now: datetime) -> str | None:
        if self.mode == "always":
            return "always"
        if self.mode in {"idle", "both"} and now > updated_at + timedelta(minutes=self.idle_minutes):
            return "idle"
        if self.mode in {"daily", "both"}:
            boundary = now.replace(hour=self.at_hour, minute=0, second=0, microsecond=0)
            if now < boundary:
                boundary -= timedelta(days=1)
            if updated_at < boundary:
                return "daily"
        return None


class GatewayNewSessionMixin:
    def _load_new_session_policy(self, source):
        from gateway.run import _load_gateway_config

        return NewSessionPolicy.from_config(
            _load_gateway_config(),
            self._load_source_profile(source) or getattr(source, "profile", None),
        )

    def _post_task_new_button_policy(self, source, agent_result):
        from gateway.run import _should_clear_resume_pending_after_turn

        if source.platform != Platform.TELEGRAM or not _should_clear_resume_pending_after_turn(agent_result):
            return None
        if not str(agent_result.get("final_response") or "").strip() and not agent_result.get("response_previewed"):
            return None
        policy = self._load_new_session_policy(source)
        return policy if policy.post_task_new_button else None

    async def _send_post_task_new_button_now(self, *, source, agent_result, reply_to_message_id=None, metadata=None):
        policy = self._post_task_new_button_policy(source, agent_result)
        adapter = self._adapter_for_source(source)
        if policy is None or not callable(getattr(adapter, "send_new_session_button", None)):
            return False
        result = await adapter.send_new_session_button(
            source.chat_id, text=policy.post_task_new_button_text,
            button_label=policy.post_task_new_button_label,
            metadata=metadata if metadata is not None else self._thread_metadata_for_source(source, reply_to_message_id),
        )
        return result is None or result.success

    async def _offer_post_task_new_button_for_kanban_completion(self, *, adapter, platform, sub, msg):
        from gateway.session import SessionSource

        if not callable(getattr(adapter, "send_new_session_button", None)):
            return False
        metadata = dict(sub.get("delivery_metadata") or {})
        if sub.get("thread_id"):
            metadata.setdefault("thread_id", sub["thread_id"])
        source = SessionSource(
            platform=platform, chat_id=sub["chat_id"],
            chat_type=sub.get("chat_type") or metadata.get("chat_type") or "group",
            thread_id=sub.get("thread_id") or None, user_id=sub.get("user_id"),
            profile=sub.get("notifier_profile") or None,
        )
        source._transport_adapter_ref = weakref.ref(adapter)
        with self._profile_scope_for_source(source):
            return await self._send_post_task_new_button_now(
                source=source, agent_result={"final_response": msg, "completed": True}, metadata=metadata,
            )

    def _register_post_task_new_button(self, *, source, session_key, run_generation, agent_result):
        policy = self._post_task_new_button_policy(source, agent_result)
        adapter = self._adapter_for_source(source)
        if policy is None or not callable(getattr(adapter, "send_new_session_button", None)):
            return
        if not callable(getattr(adapter, "register_post_delivery_callback", None)):
            return
        # copy preserves build_source's transport provenance, unlike dataclasses.replace.
        snapshot = copy.copy(source)

        async def send_button():
            with self._profile_scope_for_source(snapshot):
                await adapter.send_new_session_button(
                    snapshot.chat_id, text=policy.post_task_new_button_text,
                    button_label=policy.post_task_new_button_label,
                    metadata=self._thread_metadata_for_source(snapshot),
                )

        adapter.register_post_delivery_callback(session_key, send_button, generation=run_generation)
