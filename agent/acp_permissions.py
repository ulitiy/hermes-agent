"""Local ACP approval bridge; generic ACP clients remain fail-closed by default."""

import json
import logging
from typing import Any

from agent.acp_client import _jsonrpc_result
from tools.approval import request_acp_permission_approval

logger = logging.getLogger(__name__)


def _permission_denied(message_id: Any) -> dict[str, Any]:
    return _jsonrpc_result(message_id, {"outcome": {"outcome": "cancelled"}})


def _permission_selected(message_id: Any, option_id: str) -> dict[str, Any]:
    return _jsonrpc_result(message_id, {"outcome": {"outcome": "selected", "optionId": option_id}})


def _option_id(option: dict[str, Any]) -> str:
    return str(option.get("optionId") or option.get("option_id") or "").strip()


def _option_kind(option: dict[str, Any]) -> str:
    return str(option.get("kind") or "").strip()


def _find_permission_option(options: list[dict[str, Any]], *preferred_ids: str) -> str | None:
    preferred = {value for value in preferred_ids if value}
    for option in options:
        option_id = _option_id(option)
        if option_id in preferred:
            return option_id
    return None


def _find_permission_option_by_kind(
    options: list[dict[str, Any]], kind: str, *, exclude_ids: set[str] | None = None,
) -> str | None:
    exclude_ids = exclude_ids or set()
    for option in options:
        option_id = _option_id(option)
        if option_id and option_id not in exclude_ids and _option_kind(option) == kind:
            return option_id
    return None


def _select_permission_option(options: list[dict[str, Any]], choice: str) -> str | None:
    if not options or choice == "cancel":
        return None
    if choice == "deny":
        return (
            _find_permission_option(options, "reject_once", "decline", "deny", "reject_permissions")
            or _find_permission_option_by_kind(options, "reject_once")
            or _find_permission_option_by_kind(options, "reject_always")
        )
    if choice == "once":
        return (_find_permission_option(options, "allow_once", "accept")
                or _find_permission_option_by_kind(options, "allow_once"))
    if choice == "session":
        return (
            _find_permission_option(options, "allow_session", "allow_permissions_session", "allow_always")
            or _find_permission_option_by_kind(options, "allow_always", exclude_ids={"accept_execpolicy_amendment"})
            or _find_permission_option(options, "allow_once", "accept")
            or _find_permission_option_by_kind(options, "allow_once")
        )
    if choice == "always":
        return (
            _find_permission_option(options, "accept_execpolicy_amendment")
            or next((_option_id(option) for option in options
                     if _option_id(option).startswith("apply_network_policy_amendment")
                     and _option_kind(option) == "allow_always"), None)
            or _find_permission_option(options, "allow_always", "allow_session", "allow_permissions_session")
            or _find_permission_option_by_kind(options, "allow_always")
            or _find_permission_option(options, "allow_once", "accept")
            or _find_permission_option_by_kind(options, "allow_once")
        )
    return None


def _permission_content_text(tool_call: dict[str, Any]) -> str:
    parts: list[str] = []
    content = tool_call.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            block = item.get("content") if isinstance(item.get("content"), dict) else item
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n\n".join(parts)


def _format_permission_request(params: dict[str, Any]) -> tuple[str, str, str]:
    tool_call = params.get("toolCall") or params.get("tool_call") or {}
    if not isinstance(tool_call, dict):
        tool_call = {}
    raw_input = tool_call.get("rawInput") or tool_call.get("raw_input") or {}
    if not isinstance(raw_input, dict):
        raw_input = {}
    raw_meta = params.get("_meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_codex_meta = meta.get("codex")
    codex_meta = raw_codex_meta if isinstance(raw_codex_meta, dict) else {}
    raw_codex_params = codex_meta.get("params")
    codex_params = raw_codex_params if isinstance(raw_codex_params, dict) else {}
    kind = str(tool_call.get("kind") or codex_params.get("type") or "permission").strip() or "permission"
    title = str(tool_call.get("title") or codex_params.get("reason") or "").strip()
    command = str(raw_input.get("command") or codex_params.get("command") or "").strip()
    cwd = str(raw_input.get("cwd") or codex_params.get("cwd") or "").strip()
    content_text = _permission_content_text(tool_call)
    if command:
        display_command = f"$ cd {cwd}\n$ {command}" if cwd else command
    elif content_text:
        display_command = content_text
    elif title:
        display_command = title
    else:
        display_command = json.dumps(raw_input or codex_params or params, ensure_ascii=False, default=str)
    kind_description = {
        "execute": "run a command", "edit": "apply a file edit",
        "fetch": "fetch external content", "other": "perform an external action",
    }.get(kind, f"perform {kind}")
    description_parts = [f"Codex ACP requests permission to {kind_description}"]
    if title and title not in display_command:
        description_parts.append(title)
    if content_text and content_text not in display_command:
        description_parts.append(content_text)
    return display_command, " — ".join(description_parts), f"acp_permission:{kind}"


def _resolve_permission_request(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    options = params.get("options")
    if not isinstance(options, list) or not options:
        return _permission_denied(message_id)
    normalized_options = [option for option in options if isinstance(option, dict) and _option_id(option)]
    if not normalized_options:
        return _permission_denied(message_id)
    command, description, pattern_key = _format_permission_request(params)
    session_option = _select_permission_option(normalized_options, "session")
    always_option = _select_permission_option(normalized_options, "always")
    try:
        choice = request_acp_permission_approval(
            command=command, description=description, pattern_key=pattern_key,
            allow_permanent=bool(always_option and always_option != session_option),
            surface="codex-acp-permission",
        )
    except Exception:
        logger.exception("ACP permission approval bridge failed")
        return _permission_denied(message_id)
    option_id = _select_permission_option(normalized_options, choice)
    return _permission_selected(message_id, option_id) if option_id else _permission_denied(message_id)
