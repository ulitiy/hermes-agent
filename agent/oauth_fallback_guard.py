"""Keep persistent OAuth authentication failures from silently becoming paid usage."""

from agent.error_classifier import FailoverReason


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
