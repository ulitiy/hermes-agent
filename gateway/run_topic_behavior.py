"""Topic role/model/effort overlays; transport ownership always stays on ``source.profile``.

These helpers never bind a home or secret scope. Call them inside the transport's
existing runtime scope; memory, skills and session storage remain there.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from hermes_constants import parse_reasoning_effort, resolve_reasoning_config

if TYPE_CHECKING:
    from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


def _source_mapping_entries(source: SessionSource, names: tuple[str, ...]):
    from gateway.run import _load_gateway_config, _platform_config_key

    platform = source.platform
    try:
        platform_key = _platform_config_key(platform)
    except (AttributeError, ValueError):
        platform_key = str(getattr(platform, "value", platform) or "")
    platform_cfg = _load_gateway_config().get(platform_key)
    if not isinstance(platform_cfg, dict):
        return
    chat, thread, topic = (
        str(getattr(source, name, "") or "").strip()
        for name in ("chat_id", "thread_id", "chat_topic")
    )
    candidates = ([f"{chat}:{thread}"] if chat and thread else [])
    candidates += [thread, f"thread:{thread}"] if thread else []
    candidates += [topic, topic.lower()] if topic else []
    candidates += [chat] if chat else []
    for name in names:
        mapping = platform_cfg.get(name)
        if isinstance(mapping, dict):
            for key in candidates:
                if key in mapping:
                    yield platform_key, name, key, mapping[key]


def build_turn_ephemeral_prompt(runner, ctx) -> str:
    """Platform context, channel prompt, configured ephemeral prompt, then active role."""
    combined = ctx.context_prompt or ""
    for extra in (
        (ctx.channel_prompt or "").strip(),
        runner._get_system_prompt_for_channel(
            ctx.source.platform, ctx.source.chat_id or "", thread_id=getattr(ctx.source, "thread_id", None),
            parent_id=getattr(ctx.source, "parent_chat_id", None),
        ),
    ):
        if extra:
            combined = (combined + "\n\n" + extra).strip()
    profile_name = runner._load_source_profile(ctx.source)
    if profile_name:
        soul = runner._load_profile_soul(profile_name)
        if soul:
            combined = (combined + f"\n\n## Active role profile: {profile_name}\n\n{soul}").strip()
    return combined


def apply_profile_channel_runtime(runner, source, model, runtime_kwargs):
    """Topic role then channel overlay, only when no session override is active."""
    from gateway.run import _get_channel_override, _resolve_runtime_agent_kwargs_for_provider

    profile_name = runner._load_source_profile(source) if source is not None else None
    if profile_name:
        profile_model, profile_runtime = runner._load_profile_model_runtime_config(profile_name)
        if profile_model:
            model = profile_model
        if profile_runtime:
            runtime_kwargs = profile_runtime
    cfg = getattr(runner, "config", None)
    if cfg and source is not None:
        channel = _get_channel_override(
            cfg, source.platform, str(source.chat_id) if source.chat_id else "",
            thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
            parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
        )
        if channel:
            if channel.model:
                model = channel.model
            if channel.provider:
                runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(channel.provider, target_model=model or None)
                runtime_model = runtime_kwargs.pop("model", None)
                if runtime_model and not channel.model:
                    model = runtime_model
    return model, runtime_kwargs


class GatewayTopicBehaviorMixin:
    """Legacy topic controls, using upstream's call-time, profile-scoped loaders."""

    @staticmethod
    def _load_source_profile(source: SessionSource | None) -> str | None:
        if source is None:
            return None
        bound = getattr(source, "behavior_profile", None)
        if bound:
            return str(bound)
        from hermes_cli.profiles import normalize_profile_name

        for platform, mapping, key, raw in _source_mapping_entries(source, (
            "profile_by_thread", "profile_overrides", "topic_profile", "thread_profile",
        )):
            if isinstance(raw, dict):
                raw = raw.get("profile") or raw.get("name")
            name = str(raw or "").strip()
            if not name:
                continue
            try:
                return normalize_profile_name(name)
            except ValueError:
                logger.warning("Invalid %s.%s[%r] profile %r, ignoring", platform, mapping, key, raw)
        return None

    @staticmethod
    def _load_source_reasoning_config(source: SessionSource | None) -> dict | None:
        if source is None:
            return None
        for platform, mapping, key, raw in _source_mapping_entries(source, (
            "reasoning_effort_by_thread", "topic_reasoning_effort",
            "thread_reasoning_effort", "reasoning_effort_overrides",
        )):
            if isinstance(raw, dict):
                raw = raw.get("effort", raw.get("reasoning_effort"))
            # Keep YAML false intact: upstream treats it as explicitly disabled.
            parsed = parse_reasoning_effort(raw)
            if parsed is not None:
                return parsed
            logger.warning("Unknown %s.%s[%r] reasoning_effort %r, ignoring", platform, mapping, key, raw)
        return None

    @staticmethod
    def _profile_dir(profile_name: str) -> Path | None:
        from gateway.run import _gateway_config_home
        from hermes_cli.profiles import normalize_profile_name

        if not profile_name:
            return None
        try:
            canon = normalize_profile_name(profile_name)
        except ValueError:
            return None
        if canon == "default":
            return None
        # The transport may be multiplexed; the launch-time _hermes_home is not
        # its owner. Resolve siblings relative to the CURRENT transport scope.
        home = _gateway_config_home()
        root = home.parent.parent if home.parent.name == "profiles" else home
        pdir = root / "profiles" / canon
        try:
            return pdir if pdir.is_dir() else None
        except OSError:
            return None

    @classmethod
    def _load_topic_profile_config(cls, profile_name: str) -> dict:
        from gateway.run import _load_gateway_config

        pdir = cls._profile_dir(profile_name)
        # Explicit path, not a runtime-scope swap. Expansion uses transport secrets.
        return _load_gateway_config(pdir / "config.yaml") if pdir is not None else {}

    @classmethod
    def _load_profile_reasoning_config(cls, profile_name: str, model: str = "") -> dict | None:
        """Profile effort, including upstream per-model overrides and YAML false."""
        return resolve_reasoning_config(cls._load_topic_profile_config(profile_name), model)

    @classmethod
    def _load_profile_soul(cls, profile_name: str) -> str | None:
        pdir = cls._profile_dir(profile_name)
        if pdir is None:
            return None
        try:
            return (pdir / "SOUL.md").read_text(encoding="utf-8-sig").strip() or None
        except (OSError, UnicodeError):
            logger.debug("Could not read profile SOUL from %s", pdir, exc_info=True)
            return None

    @classmethod
    def _load_profile_model_runtime_config(cls, profile_name: str) -> tuple[str | None, dict | None]:
        """The final old stack's actual helper: return ``(model, agent_kwargs)``.

        Only the mapped model block is an overlay. Provider credential discovery
        stays in the transport scope; do not load the role's .env/auth/home.
        """
        from gateway.run import _runtime_agent_kwargs
        from hermes_cli.runtime_provider import format_runtime_provider_error, resolve_runtime_provider

        model_cfg = cls._load_topic_profile_config(profile_name).get("model")
        if isinstance(model_cfg, str):
            return model_cfg.strip() or None, None
        if not isinstance(model_cfg, dict):
            return None, None
        model = str(model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name") or "").strip() or None
        provider = str(model_cfg.get("provider") or "").strip()
        if not provider:
            return model, None
        try:
            runtime = resolve_runtime_provider(
                requested=provider,
                explicit_api_key=str(model_cfg.get("api_key") or "").strip() or None,
                explicit_base_url=str(model_cfg.get("base_url") or "").strip() or None,
                target_model=model,
            )
        except Exception as exc:
            raise RuntimeError(format_runtime_provider_error(exc)) from exc
        kwargs = {
            **_runtime_agent_kwargs(runtime),
            "request_overrides": dict(runtime.get("request_overrides") or {}),
            "capabilities": dict(runtime.get("capabilities") or {}),
        }
        api_mode = str(model_cfg.get("api_mode") or model_cfg.get("openai_runtime") or "").strip()
        if api_mode:
            kwargs["api_mode"] = api_mode
        max_tokens = model_cfg.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            kwargs["max_tokens"] = max_tokens
        return model, kwargs

    @classmethod
    def _load_profile_runtime_provider(cls, profile_name: str) -> tuple[str | None, dict | None]:
        """Topic overlay entry point; retain the legacy model/runtime tuple contract."""
        return cls._load_profile_model_runtime_config(profile_name)

    @staticmethod
    def _unwrap_voice_transcript(text: str) -> tuple[str | None, str]:
        """Unwrap the legacy STT envelope, retaining appended reply/context blocks."""
        if not isinstance(text, str):
            return None, ""
        match = re.match(
            r'^\[The user sent a voice message~ Here\'s what they said: "(?P<transcript>.*?)"\]'
            r'(?P<tail>[\s\S]*)$', text.lstrip(), flags=re.DOTALL,
        )
        return (match.group("transcript"), match.group("tail").lstrip()) if match else (None, "")

    @classmethod
    def _parse_turn_reasoning_prefix(cls, text: str) -> tuple[str | None, str]:
        """Strip a deterministic one-turn prefix, including inside a voice envelope."""
        if not isinstance(text, str):
            return None, text
        stripped = text.lstrip()
        if not stripped or stripped.startswith("/"):
            return None, text
        transcript, tail = cls._unwrap_voice_transcript(stripped)
        if transcript is not None:
            mode, rest = cls._parse_turn_reasoning_prefix(transcript)
            if mode:
                return mode, f"{rest}\n\n{tail}" if tail else rest
        match = re.match(
            r"^(?P<prefix>light|lite|лайт|heavy|хеви|хэви)"
            r"(?:\s*[:：,;.!?\-–—]+)?\s+(?P<rest>\S[\s\S]*)$",
            stripped, flags=re.IGNORECASE,
        )
        if not match:
            return None, text
        mode = "light" if match.group("prefix").lower() in {"light", "lite", "лайт"} else "heavy"
        return mode, match.group("rest")

    @staticmethod
    def _apply_turn_reasoning_override(reasoning_config: dict | None, turn_reasoning_mode: str | None) -> dict | None:
        if not turn_reasoning_mode:
            return reasoning_config
        return parse_reasoning_effort("low" if turn_reasoning_mode == "light" else "xhigh")
