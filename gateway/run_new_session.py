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


async def reserve_native_route(authority, source, *, principal_id, request_id, fence=None):
    """Apply opt-in policy before the native FIFO chooses its owner, never on replay.

    The route admission lock already orders native inputs. An existing owner's mutation/claim
    locks also fence app submissions and claims during the policy decision. Keep those locks in
    the tracked task so cancellation cannot release them while the store transition is running.
    """
    import asyncio
    from contextlib import nullcontext
    from gateway.session_runtime_workers import track_mutation
    from hermes_state_runtime import find_session_admission, list_session_admissions

    store = authority.runner.session_store
    load_policy = getattr(authority.runner, '_load_new_session_policy', None)

    def reserve(policy):
        if fence is not None:
            fence()
        if policy is not None and policy.mode != 'none':
            entry = store.lookup_by_session_key(store._generate_session_key(source))
            if entry is not None:
                owner = authority.logical_owner(entry.session_id)
                # Queued/unknown input owns its conversation just as a running claim does.
                # A terminal redelivery must reach the ledger's digest check, not a new owner.
                if (list_session_admissions(authority.db, session_id=owner)
                        or find_session_admission(authority.db, principal_id=principal_id,
                                                  session_id=owner, request_id=request_id)
                        or store._local_receipt_target(entry) is not None or store._api_bound(entry)):
                    policy = None
        if policy is None or policy.mode == 'none':
            return authority._reserve_route(source)
        return authority._reserve_route(source, reset_policy=policy)

    async def run():
        policy = (await asyncio.to_thread(load_policy, source)
                  if callable(load_policy) and source.platform not in (Platform.LOCAL, Platform.API_SERVER)
                  else None)
        # Default-off deployments retain the old reservation path without extra store reads.
        if policy is None or policy.mode == 'none':
            return await asyncio.to_thread(reserve, None)
        entry = await asyncio.to_thread(store.lookup_by_session_key, store._generate_session_key(source))
        owner = await asyncio.to_thread(authority.logical_owner, entry.session_id) if entry else None
        live = authority.sessions.get(owner)
        # Compression holds claim_gate across an LLM call: busy input must still commit,
        # but cannot rotate that conversation. Free locks are acquired below without yielding.
        if live is not None and (live.claim_gate.locked() or live.mutation_lock.locked()):
            return await asyncio.to_thread(reserve, None)
        # Match _claim_next's order; reversing it deadlocks admission against a claim.
        async with live.claim_gate if live is not None else nullcontext():
            async with live.mutation_lock if live is not None else nullcontext():
                return await asyncio.to_thread(reserve, policy)

    return await asyncio.shield(track_mutation(authority, run()))


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

    async def _send_post_task_new_button_now(self, *, source, agent_result, reply_to_message_id=None, metadata=None, adapter=None):
        policy = self._post_task_new_button_policy(source, agent_result)
        if adapter is None:
            adapter = self._delivery_adapter_for(source)
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
                source=source, agent_result={"final_response": msg, "completed": True}, metadata=metadata, adapter=adapter,
            )

    def _register_post_task_new_button(self, *, source, session_key, run_generation, agent_result):
        policy = self._post_task_new_button_policy(source, agent_result)
        adapter = self._delivery_adapter_for(source)
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
