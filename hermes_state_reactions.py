"""Opt-in Telegram feedback storage; no tables are created on SessionDB startup."""

import sqlite3
import time
from typing import Any, Dict, List, Optional


class SessionReactionsMixin:
    _REACTION_SNIPPET_MAX = 240
    _OUTBOUND_INDEX_MAX_AGE_SECONDS = 90 * 24 * 3600

    def apply_telegram_reactions_migration(self) -> None:
        def _do(conn):
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_outbound_messages (
                    chat_id TEXT NOT NULL, message_id TEXT NOT NULL, thread_id TEXT,
                    session_id TEXT, snippet TEXT, sent_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_outbound_sent_at
                    ON telegram_outbound_messages(sent_at);
                CREATE TABLE IF NOT EXISTS telegram_reaction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
                    thread_id TEXT, message_id TEXT NOT NULL, user_id TEXT, user_name TEXT,
                    emoji TEXT NOT NULL, reaction_kind TEXT NOT NULL DEFAULT 'emoji',
                    action TEXT NOT NULL DEFAULT 'add', session_id TEXT, snippet TEXT,
                    created_at REAL NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_reaction_events_created
                    ON telegram_reaction_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_telegram_reaction_events_review
                    ON telegram_reaction_events(reviewed, created_at);
            """)
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("telegram_reactions_schema_version", "1"),
            )
        self._execute_write(_do)

    def record_telegram_outbound_message(
        self, *, chat_id: str, message_id: str, thread_id: Optional[str] = None,
        session_id: Optional[str] = None, snippet: Optional[str] = None,
        sent_at: Optional[float] = None,
    ) -> None:
        self.apply_telegram_reactions_migration()
        now = sent_at if sent_at is not None else time.time()
        snip = " ".join(str(snippet).split())[:self._REACTION_SNIPPET_MAX] if snippet else None

        def _do(conn):
            conn.execute("""
                INSERT INTO telegram_outbound_messages
                    (chat_id, message_id, thread_id, session_id, snippet, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    session_id = COALESCE(excluded.session_id, telegram_outbound_messages.session_id),
                    snippet = COALESCE(excluded.snippet, telegram_outbound_messages.snippet),
                    sent_at = excluded.sent_at
                """, (str(chat_id), str(message_id), str(thread_id) if thread_id is not None else None,
                      str(session_id) if session_id is not None else None, snip, now))
            conn.execute("DELETE FROM telegram_outbound_messages WHERE sent_at < ?",
                         (now - self._OUTBOUND_INDEX_MAX_AGE_SECONDS,))
        self._execute_write(_do)

    def lookup_telegram_outbound_message(self, *, chat_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT chat_id, message_id, thread_id, session_id, snippet, sent_at "
                    "FROM telegram_outbound_messages WHERE chat_id = ? AND message_id = ?",
                    (str(chat_id), str(message_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def record_telegram_reaction(
        self, *, chat_id: str, message_id: str, emoji: str, thread_id: Optional[str] = None,
        user_id: Optional[str] = None, user_name: Optional[str] = None,
        reaction_kind: str = "emoji", action: str = "add", session_id: Optional[str] = None,
        snippet: Optional[str] = None, created_at: Optional[float] = None,
    ) -> int:
        self.apply_telegram_reactions_migration()
        now = created_at if created_at is not None else time.time()
        if session_id is None or snippet is None:
            indexed = self.lookup_telegram_outbound_message(chat_id=chat_id, message_id=message_id)
            if indexed:
                if session_id is None:
                    session_id = indexed.get("session_id")
                if snippet is None:
                    snippet = indexed.get("snippet")
                if thread_id is None:
                    thread_id = indexed.get("thread_id")
        snip = " ".join(str(snippet).split())[:self._REACTION_SNIPPET_MAX] if snippet else None

        def _do(conn):
            cur = conn.execute("""
                INSERT INTO telegram_reaction_events
                    (chat_id, thread_id, message_id, user_id, user_name, emoji, reaction_kind,
                     action, session_id, snippet, created_at, reviewed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """, (str(chat_id), str(thread_id) if thread_id is not None else None,
                      str(message_id), str(user_id) if user_id is not None else None,
                      str(user_name) if user_name is not None else None, str(emoji),
                      str(reaction_kind or "emoji"), str(action or "add"),
                      str(session_id) if session_id is not None else None, snip, now))
            return int(cur.lastrowid)
        return self._execute_write(_do)

    def list_telegram_reactions(
        self, *, since_ts: Optional[float] = None, only_unreviewed: bool = False,
        actions: Optional[tuple] = ("add",), limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("created_at >= ?")
            params.append(float(since_ts))
        if only_unreviewed:
            clauses.append("reviewed = 0")
        if actions:
            clauses.append("action IN (" + ",".join("?" for _ in actions) + ")")
            params.extend(str(a) for a in actions)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM telegram_reaction_events" + where
                    + " ORDER BY created_at DESC, id DESC LIMIT ?", (*params, int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def mark_telegram_reactions_reviewed(self, ids: List[int]) -> int:
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return 0
        self.apply_telegram_reactions_migration()

        def _do(conn):
            placeholders = ",".join("?" for _ in ids)
            return conn.execute(
                f"UPDATE telegram_reaction_events SET reviewed = 1 WHERE id IN ({placeholders})", ids,
            ).rowcount
        return self._execute_write(_do)
