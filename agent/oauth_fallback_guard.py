"""Keep persistent OAuth authentication failures from silently becoming paid usage."""

import logging

from agent.error_classifier import FailoverReason


def _fallback_entry_unavailable_without_network(agent, fb: dict) -> str | None:
    """Skip fallback routes that have no local auth, without making a network probe."""
    if (fb.get("provider") or "").strip().lower() != "nous":
        return None
    try:
        from hermes_cli.auth import get_provider_auth_state
        state = get_provider_auth_state("nous") or {}
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Fallback auth state unreadable",
            exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__),
        )
        return f"nous_auth_unreadable:{type(exc).__name__}"
    has_token = any(isinstance(t, str) and t.strip() for t in (state.get("access_token"), state.get("refresh_token")))
    return None if has_token else "nous_token_missing"


def _should_block_paid_oauth_auth_fallback(
    agent, *, reason, current_provider: str, fallback_provider: str,
) -> bool:
    return (
        reason == FailoverReason.auth
        and current_provider in {"openai-codex", "xai-oauth"}
        and fallback_provider in {"anthropic", "bedrock"}
    )


def _should_retry_primary_auth_before_fallback(
    agent, classified, *, status_code, already_attempted: bool,
) -> bool:
    if already_attempted or status_code != 401 or not classified.is_auth:
        return False
    if (getattr(agent, "provider", "") or "").strip().lower() not in {
        "openai-codex", "xai-oauth", "nous", "copilot",
    }:
        return False
    pending = getattr(agent, "_has_pending_fallback", None)
    if callable(pending):
        return bool(pending())
    return (getattr(agent, "_fallback_index", 0) or 0) < len(
        getattr(agent, "_fallback_chain", None) or []
    )
