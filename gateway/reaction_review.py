"""Weekly review of Telegram reactions placed on Hermes messages.

This module turns the raw ``telegram_reaction_events`` rows (captured by the
Telegram adapter's ``MessageReactionHandler`` — see
``gateway/platforms/telegram.py``) into a human-readable feedback digest that
asks Sasha targeted follow-ups, e.g. "you put 💩 here — what went wrong?" or
"you put ❤️ here — what was useful?".

It is deliberately a plain, dependency-light module so it can be:
  * imported by the ``scripts/reaction_digest.py`` CLI (cron data-collection),
  * unit-tested directly against a temp SessionDB, and
  * reused from a gateway slash command or an agent tool later.

Privacy: the digest only surfaces the assistant's own output snippet (already
truncated at capture time) plus the emoji + reactor name. No inbound user text
and no raw Telegram payloads are stored or rendered.
"""

from __future__ import annotations

import time
from typing import Any, Optional


# Sentiment buckets for the common Telegram reaction set. The mapping is
# intentionally small and conservative — anything unrecognised lands in
# "other" so the digest still surfaces it for a human to interpret.
POSITIVE_EMOJI = {
    "👍", "❤", "❤️", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "🙏", "👌",
    "💯", "🤝", "🏆", "❤️‍🔥", "😍", "🤗", "✍", "✍️", "🆒", "💘", "😎",
    "🫡", "🤓", "⚡", "🕊", "🕊️",
}
NEGATIVE_EMOJI = {
    "👎", "💩", "🤬", "😢", "😭", "🤮", "😡", "🤨", "😐", "🥱", "🥴",
    "💔", "🖕", "😈", "🤡", "🤯", "😱", "🙄",
}
# "thinking"-style — ambiguous, worth a follow-up but neither praise nor pan.
NEUTRAL_EMOJI = {"🤔", "🌚", "👀", "🙈", "🙊", "🗿", "😨", "🤷", "🤷‍♂️", "🤷‍♀️"}

SENTIMENT_PROMPT = {
    "positive": "what was useful here?",
    "negative": "what went wrong here?",
    "neutral": "what were you reacting to here?",
    "other": "what did this reaction mean?",
}

DEFAULT_SINCE_DAYS = 7


def classify_emoji(emoji: str) -> str:
    """Return 'positive' | 'negative' | 'neutral' | 'other' for an emoji."""
    if emoji in POSITIVE_EMOJI:
        return "positive"
    if emoji in NEGATIVE_EMOJI:
        return "negative"
    if emoji in NEUTRAL_EMOJI:
        return "neutral"
    return "other"


def _snippet_or_placeholder(row: dict[str, Any]) -> str:
    snip = (row.get("snippet") or "").strip()
    if snip:
        return snip
    # No snippet means the bot message predates the outbound index (or the
    # index entry was pruned). Still worth flagging so Sasha can recall it.
    return "(no stored text — message predates capture or was pruned)"


def collect_reactions(
    db: Any,
    *,
    since_days: int = DEFAULT_SINCE_DAYS,
    only_unreviewed: bool = True,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read 'add' reaction events from the SessionDB for the review window."""
    since_ts = time.time() - max(since_days, 0) * 24 * 3600 if since_days else None
    return db.list_telegram_reactions(
        since_ts=since_ts,
        only_unreviewed=only_unreviewed,
        actions=("add",),
        limit=limit,
    )


def build_digest(
    db: Any,
    *,
    since_days: int = DEFAULT_SINCE_DAYS,
    only_unreviewed: bool = True,
    mark_reviewed: bool = False,
    max_items_per_bucket: int = 8,
    limit: int = 500,
) -> dict[str, Any]:
    """Build a structured + rendered weekly reaction digest.

    Returns a dict with:
      ``total``        — number of reaction events in the window
      ``counts``       — {emoji: n} frequency table
      ``buckets``      — {sentiment: [item, ...]} where each item carries the
                          emoji, snippet, session_id, chat/message ids, ts
      ``followups``    — flat list of targeted follow-up question strings
      ``text``         — a ready-to-send markdown digest (or empty-state note)
      ``reviewed_ids`` — ids marked reviewed (only when mark_reviewed=True)
      ``empty``        — True when there is nothing to review
    """
    rows = collect_reactions(
        db, since_days=since_days, only_unreviewed=only_unreviewed, limit=limit
    )

    counts: dict[str, int] = {}
    buckets: dict[str, list[dict[str, Any]]] = {
        "negative": [], "positive": [], "neutral": [], "other": [],
    }
    for r in rows:
        emoji = r.get("emoji") or "?"
        counts[emoji] = counts.get(emoji, 0) + 1
        sentiment = classify_emoji(emoji)
        buckets[sentiment].append(
            {
                "id": r.get("id"),
                "emoji": emoji,
                "sentiment": sentiment,
                "snippet": _snippet_or_placeholder(r),
                "session_id": r.get("session_id"),
                "chat_id": r.get("chat_id"),
                "message_id": r.get("message_id"),
                "user_name": r.get("user_name"),
                "created_at": r.get("created_at"),
            }
        )

    followups: list[str] = []
    # Surface negatives first (highest learning value), then neutral, positive.
    for sentiment in ("negative", "neutral", "positive", "other"):
        items = buckets[sentiment][:max_items_per_bucket]
        question = SENTIMENT_PROMPT[sentiment]
        for item in items:
            snippet = item["snippet"]
            if len(snippet) > 160:
                snippet = snippet[:157].rstrip() + "…"
            followups.append(
                f'You put {item["emoji"]} on: "{snippet}" — {question}'
            )

    reviewed_ids: list[int] = []
    if mark_reviewed and rows:
        ids = [r["id"] for r in rows if r.get("id") is not None]
        db.mark_telegram_reactions_reviewed(ids)
        reviewed_ids = ids

    text = render_digest_text(
        total=len(rows),
        counts=counts,
        buckets=buckets,
        since_days=since_days,
        max_items_per_bucket=max_items_per_bucket,
    )

    return {
        "total": len(rows),
        "counts": counts,
        "buckets": buckets,
        "followups": followups,
        "text": text,
        "reviewed_ids": reviewed_ids,
        "empty": len(rows) == 0,
        "since_days": since_days,
    }


def render_digest_text(
    *,
    total: int,
    counts: dict[str, int],
    buckets: dict[str, list[dict[str, Any]]],
    since_days: int,
    max_items_per_bucket: int = 8,
) -> str:
    """Render a compact plain-text digest suitable for a Telegram message."""
    if total == 0:
        return (
            f"No new Telegram reactions in the last {since_days} day(s). "
            "Nothing to review."
        )

    # Frequency summary line, most-used emoji first.
    freq = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    freq_line = "  ".join(f"{emoji}×{n}" for emoji, n in freq)

    lines: list[str] = []
    lines.append(f"🗳 Weekly reaction review — last {since_days} day(s)")
    lines.append(f"Total reactions: {total}")
    lines.append(f"Breakdown: {freq_line}")
    lines.append("")

    section_titles = {
        "negative": "👎 Negative — what went wrong?",
        "neutral": "🤔 Ambiguous — what were you reacting to?",
        "positive": "❤️ Positive — what was useful?",
        "other": "❓ Other reactions",
    }
    for sentiment in ("negative", "neutral", "positive", "other"):
        items = buckets.get(sentiment, [])[:max_items_per_bucket]
        if not items:
            continue
        lines.append(section_titles[sentiment])
        for item in items:
            snippet = item["snippet"]
            if len(snippet) > 160:
                snippet = snippet[:157].rstrip() + "…"
            lines.append(f'  {item["emoji"]} "{snippet}"')
        lines.append("")

    lines.append(
        "Reply with what each reaction meant so I can adjust future behavior."
    )
    return "\n".join(lines).rstrip()


def open_session_db(db_path: Optional[str] = None):
    """Open a SessionDB, honoring an explicit path or the active profile home.

    ``db_path`` overrides everything. Otherwise SessionDB resolves
    ``get_hermes_home() / 'state.db'`` for the active profile — so a cron job
    must run under the same profile as the gateway that captured the reactions.
    """
    from hermes_state import SessionDB

    if db_path:
        from pathlib import Path

        return SessionDB(db_path=Path(db_path))
    return SessionDB()
