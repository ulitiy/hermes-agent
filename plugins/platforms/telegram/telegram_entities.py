"""Telegram ``text_link`` expansion, kept out of the adapter facade.

A rich-text hyperlink arrives as visible anchor text plus a ``text_link`` entity carrying the
URL; the model only ever sees the anchor unless the URL is inlined (#31071).
"""

from __future__ import annotations

import re
from typing import Any, Optional


_TELEGRAM_FENCED_CODE_RE = re.compile(r"```(?:[^\n]*\n)?[\s\S]*?```")
_TELEGRAM_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_TELEGRAM_TAPPABLE_SLASH_COMMAND_RE = re.compile(
    r"^/(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)"
    r"(?P<bot>@[A-Za-z0-9_]{5,32})?"
    r"(?P<args>\s+.*)?$"
)


def _normalize_tappable_telegram_command_line(line: str) -> Optional[str]:
    """Return a Telegram-tappable command line, or None if *line* is not one."""
    from hermes_cli.commands_platforms import _sanitize_telegram_name
    raw = line.strip()
    if not raw:
        return ""
    match = _TELEGRAM_TAPPABLE_SLASH_COMMAND_RE.match(raw)
    if not match:
        return None
    name = _sanitize_telegram_name(match.group("name"))
    if not name:
        return None
    return f"/{name}{match.group('bot') or ''}{match.group('args') or ''}"


def _unwrap_tappable_slash_command_code(text: str) -> str:
    """Unwrap command-only code so Telegram renders tappable slash commands.

    Preserve ordinary code verbatim. Only inline code whose entire contents are
    a command and fences whose every nonblank line is a command are rewritten.
    """
    if not text or "`" not in text or "/" not in text:
        return text

    def _unwrap_inline(segment: str) -> str:
        def _replace_inline(match: re.Match[str]) -> str:
            normalized = _normalize_tappable_telegram_command_line(match.group(1))
            return normalized if normalized is not None else match.group(0)

        return _TELEGRAM_INLINE_CODE_RE.sub(_replace_inline, segment)

    def _unwrap_fenced(block: str) -> Optional[str]:
        inner = block[3:-3]
        body = inner.split("\n", 1)[1] if "\n" in inner else inner
        if not body.strip():
            return None
        out_lines: list[str] = []
        saw_command = False
        for line in body.splitlines():
            normalized = _normalize_tappable_telegram_command_line(line)
            if normalized is None:
                return None
            if normalized:
                saw_command = True
            out_lines.append(normalized)
        if not saw_command:
            return None
        return "\n".join(out_lines)

    out: list[str] = []
    pos = 0
    for match in _TELEGRAM_FENCED_CODE_RE.finditer(text):
        out.append(_unwrap_inline(text[pos:match.start()]))
        unwrapped = _unwrap_fenced(match.group(0))
        out.append(unwrapped if unwrapped is not None else match.group(0))
        pos = match.end()
    out.append(_unwrap_inline(text[pos:]))
    return "".join(out)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _code_point_index(text: str, utf16_offset: int) -> int | None:
    """Python index for a UTF-16 code-unit offset; None when it splits a surrogate pair."""
    try:
        return len(text.encode("utf-16-le")[: utf16_offset * 2].decode("utf-16-le"))
    except UnicodeDecodeError:
        return None


def expand_link_entities(message: Any) -> str:
    """Message text (or caption) with every hidden ``text_link`` URL inlined after its anchor.

    Entity offsets are UTF-16 code units (emoji before the anchor count twice), so they are mapped
    to code-point indices before slicing. Malformed entities are skipped; an anchor that already
    reads as its own URL, or text already carrying the inline form, is left untouched.
    """
    text = getattr(message, "text", None)
    if text:
        entities = getattr(message, "entities", None) or []
    else:
        text = getattr(message, "caption", None) or ""
        entities = getattr(message, "caption_entities", None) or []
    if not text or not entities:
        return text

    utf16_length = _utf16_length(text)
    links: list[tuple[int, int, str]] = []
    for entity in entities:
        entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
        raw_url = getattr(entity, "url", None)
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        if entity_type != "text_link" or not url:
            continue
        try:
            offset = int(getattr(entity, "offset", -1))
            length = int(getattr(entity, "length", 0))
        except (TypeError, ValueError):
            continue
        if offset < 0 or length <= 0 or offset + length > utf16_length:
            continue
        start, end = _code_point_index(text, offset), _code_point_index(text, offset + length)
        if start is None or end is None or end <= start:
            continue
        if text[start:end].strip() == url:
            continue
        links.append((start, end, url))

    expanded = text
    for _start, end, url in sorted(links, reverse=True):
        inline = f" ({url})"
        if expanded[end:].startswith(inline):
            continue
        expanded = f"{expanded[:end]}{inline}{expanded[end:]}"
    return expanded
