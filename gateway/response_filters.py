"""Gateway response filtering helpers.

These helpers operate at the gateway boundary: they decide whether a completed
agent turn should be delivered to the chat, not what should be persisted in the
conversation history.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Canonical model-emitted control token for intentional silence.
SILENT_REPLY_TOKEN = "NO_REPLY"

# Exact whole-response markers that mean "the agent intentionally chose not to
# reply".  Keep this list small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})

REACTION_ONLY_REPLY_TOKEN = "REACTION_ONLY"
_REACTION_ONLY_RE = re.compile(
    r"^\s*REACTION_ONLY\s*:\s*(?P<emoji>\S+)\s*$",
    re.IGNORECASE,
)

# Telegram only accepts a finite reaction emoji set.  Keep the model-facing
# surface intentionally small: enough for Sprout acknowledgements, but not an
# arbitrary emoji transport.  Common status glyphs are normalized to standard
# Telegram reactions so a prompt can say "✅" while the Bot API receives "👍".
REACTION_ONLY_EMOJI_ALIASES = {
    "✅": "👍",
    "☑️": "👍",
    "✔️": "👍",
    "❌": "👎",
    "✖️": "👎",
    "❤️": "❤",
}
REACTION_ONLY_ALLOWED_EMOJI = frozenset({
    "👍",
    "👎",
    "👀",
    "❤",
    "🙏",
    "👌",
    "👏",
    "🎉",
    "🤝",
    "💯",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure.

    Models sometimes emit ``.NO_REPLY`` or ``*NO_REPLY*`` instead of the exact
    marker. Keep square brackets structural so malformed ``[SILENT`` does not
    become ``SILENT``.
    """
    start = 0
    end = len(text)
    while start < end and text[start] not in "[]" and unicodedata.category(text[start]).startswith("P"):
        start += 1
    while end > start and text[end - 1] not in "[]" and unicodedata.category(text[end - 1]).startswith("P"):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_silence_candidate(text)
    stripped = _strip_edge_silence_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    fallback = _canonical_silence_candidate(stripped)
    return (exact, fallback)


def _normalize_reaction_only_emoji(emoji: str) -> str | None:
    normalized = REACTION_ONLY_EMOJI_ALIASES.get(emoji, emoji)
    if normalized in REACTION_ONLY_ALLOWED_EMOJI:
        return normalized
    return None


def parse_reaction_only_response(response: Any) -> str | None:
    """Return the normalized emoji for a whole-response reaction marker.

    ``REACTION_ONLY: 👍`` is a gateway control response: the text itself must
    not be delivered, but the platform adapter may attach the parsed reaction to
    the inbound message.  Only exact short whole-response markers count — prose
    that merely mentions the marker remains ordinary text.
    """
    if not isinstance(response, str):
        return None
    stripped = response.strip()
    if not stripped or len(stripped) > 64:
        return None
    match = _REACTION_ONLY_RE.match(stripped)
    if not match:
        return None
    return _normalize_reaction_only_emoji(match.group("emoji"))


def is_reaction_only_response(response: Any) -> bool:
    return parse_reaction_only_response(response) is not None


def is_intentional_silence_response(response: Any) -> bool:
    """Return True only when ``response`` is exactly a silence marker.

    Substantive prose that merely mentions ``NO_REPLY`` or ``[SILENT]`` must be
    delivered normally.  A blank response is also not silence; blank output is
    handled by the empty-response failure path.
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False
    if len(stripped) > 64:
        return False
    return any(candidate in LIVE_GATEWAY_SILENT_MARKERS for candidate in _canonical_silence_candidates(stripped))


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns."""
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed"):
        return False
    return is_intentional_silence_response(response)


def is_gateway_control_marker_response(response: Any) -> bool:
    """Return True for whole-response gateway control markers.

    These are assistant final responses that should not be sent as visible text
    (currently intentional silence and reaction-only acknowledgement markers).
    """
    return is_intentional_silence_response(response) or is_reaction_only_response(response)


def is_partial_silence_marker(text: Any) -> bool:
    """Return True while ``text`` could still resolve to a silence marker.

    The streaming path accumulates the reply delta-by-delta and must decide,
    before the whole response is known, whether to show what it has so far.
    A buffer whose canonical form is a non-empty *prefix* of a silence marker
    (e.g. ``"NO"`` on the way to ``"NO_REPLY"``, or an exact marker that has
    not yet been terminated by stream-end) is held back so a raw marker is
    never edited onto the screen and then belatedly retracted.

    Anything that has already diverged from every marker (ordinary prose) —
    and anything longer than the marker cap — returns False so normal
    streaming resumes immediately.  This is the streaming counterpart to
    :func:`is_intentional_silence_response`, sharing the same marker set and
    canonicalization so the two never drift.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    for candidate in _canonical_silence_candidates(stripped):
        if candidate and any(marker.startswith(candidate) for marker in LIVE_GATEWAY_SILENT_MARKERS):
            return True
    return False


def is_partial_reaction_only_marker(text: Any) -> bool:
    """Return True while ``text`` could still become ``REACTION_ONLY: <emoji>``."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    compact = re.sub(r"\s+", "", stripped.upper())
    target = f"{REACTION_ONLY_REPLY_TOKEN}:"
    if target.startswith(compact):
        return True
    if not compact.startswith(target):
        return False
    emoji = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
    if not emoji:
        return True
    return _normalize_reaction_only_emoji(emoji) is not None


def is_partial_gateway_control_marker(text: Any) -> bool:
    """Streaming hold-back predicate for all gateway control markers."""
    return is_partial_silence_marker(text) or is_partial_reaction_only_marker(text)
