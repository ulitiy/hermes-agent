"""Admission policy shared by triage queues and transactional mutations."""
import json
import sqlite3


def requires_human_decision(kind: str | None) -> bool:
    # Explicit technical kinds retain upstream automatic orchestration. Legacy
    # untyped escalations have no such evidence and keep the human hold.
    return kind not in {"transient", "capability", "dependency"}


def triage_escalation_reason(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Comments/heartbeats cannot release a human-held block-loop escalation.

    Recheck under the write transaction: queue admission alone cannot fence an
    escalation that occurred while the auxiliary model was answering.
    """
    row = conn.execute(
        "SELECT e.id, e.kind, e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE t.id = ? AND t.status = 'triage' AND e.kind IN "
        "('blocked', 'dependency_wait', 'block_loop_detected', 'gave_up', "
        "'status', 'unblocked', 'specified', 'decomposed') ORDER BY e.id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or row["kind"] != "block_loop_detected":
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    kind = payload.get("kind") if isinstance(payload, dict) else None
    if not requires_human_decision(kind):
        return None
    return (
        f"{task_id}: block_loop_detected (event {row['id']}); triage requires a human "
        "decision. Do not auto-decompose or resume without explicit human direction."
    )
