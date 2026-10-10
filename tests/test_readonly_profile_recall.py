"""Read-only recall must query existing indexes without schema maintenance."""
import sqlite3

import pytest

from hermes_state import SessionDB


def test_readonly_search_uses_existing_fts(tmp_path):
    path = tmp_path / "state.db"
    writer = SessionDB(path)
    try:
        writer.create_session("indexed", source="cli")
        writer.append_message("indexed", role="user", content="recallneedle")
    finally:
        writer.close()
    before = path.read_bytes()
    reader = SessionDB(path, read_only=True)
    try:
        assert [row["session_id"] for row in reader.search_messages("recallneedle")] == ["indexed"]
        assert reader._conn is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader._conn.execute("DELETE FROM messages")
    finally:
        reader.close()
    assert path.read_bytes() == before


def test_readonly_close_does_not_request_checkpoint(tmp_path):
    path = tmp_path / "state.db"
    writer = SessionDB(path)
    writer.close()
    reader = SessionDB(path, read_only=True)
    statements = []
    assert reader._conn is not None
    reader._conn.set_trace_callback(statements.append)
    reader.close()
    assert not statements


def test_readonly_cjk_uses_trigram(tmp_path):
    path = tmp_path / "state.db"
    writer = SessionDB(path)
    try:
        if not writer._trigram_available:
            pytest.skip("SQLite has no trigram tokenizer")
        writer.create_session("cjk", source="cli")
        writer.append_message("cjk", role="user", content="请搜索数据库记忆内容")
    finally:
        writer.close()
    reader = SessionDB(path, read_only=True)
    statements = []
    assert reader._conn is not None
    reader._conn.set_trace_callback(statements.append)
    try:
        assert [r["session_id"] for r in reader.search_messages("数据库")] == ["cjk"]
        assert any("messages_fts_trigram MATCH" in s for s in statements)
    finally:
        reader.close()


def test_readonly_missing_fts_does_not_create_it(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    before = path.read_bytes()
    reader = SessionDB(path, read_only=True)
    try:
        assert reader.search_messages("recallneedle") == []
    finally:
        reader.close()
    assert path.read_bytes() == before
