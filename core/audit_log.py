"""Persistent SQLite action journal. Survives restarts and backs undo."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL,
    root          TEXT NOT NULL,
    action_type   TEXT NOT NULL,
    original_path TEXT NOT NULL,
    final_path    TEXT,
    sha256        TEXT,
    size          INTEGER,
    timestamp     TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    undone        INTEGER NOT NULL DEFAULT 0,
    undone_at     TEXT,
    undo_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_batch ON actions(batch_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ActionRecord:
    id: str
    batch_id: str
    root: str
    action_type: str
    original_path: str
    final_path: str | None
    sha256: str | None
    size: int | None
    timestamp: str
    status: str
    error: str | None
    undone: bool
    undone_at: str | None
    undo_error: str | None


class Journal:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def new_batch_id() -> str:
        return uuid.uuid4().hex[:12]

    def record(
        self,
        *,
        batch_id: str,
        root: Path,
        action_type: str,
        original_path: Path,
        final_path: Path | None,
        status: str,
        sha256: str | None = None,
        size: int | None = None,
        error: str | None = None,
    ) -> str:
        action_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO actions (id, batch_id, root, action_type, original_path, final_path, sha256, size,"
                " timestamp, status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    batch_id,
                    str(root),
                    action_type,
                    str(original_path),
                    str(final_path) if final_path else None,
                    sha256,
                    size,
                    _now(),
                    status,
                    error,
                ),
            )
        return action_id

    def get(self, action_id: str) -> ActionRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return self._to_record(row) if row else None

    def list_actions(self, limit: int = 500, root: Path | None = None) -> list[ActionRecord]:
        query = "SELECT * FROM actions"
        params: tuple = ()
        if root is not None:
            query += " WHERE root = ?"
            params = (str(root),)
        query += " ORDER BY timestamp DESC, rowid DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, limit)).fetchall()
        return [self._to_record(r) for r in rows]

    def batch_actions(self, batch_id: str) -> list[ActionRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM actions WHERE batch_id = ? ORDER BY rowid", (batch_id,)).fetchall()
        return [self._to_record(r) for r in rows]

    def mark_undone(self, action_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE actions SET undone = 1, undone_at = ?, undo_error = NULL WHERE id = ?", (_now(), action_id)
            )

    def record_undo_failure(self, action_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE actions SET undo_error = ? WHERE id = ?", (error, action_id))

    @staticmethod
    def _to_record(row: sqlite3.Row) -> ActionRecord:
        data = dict(row)
        data["undone"] = bool(data["undone"])
        return ActionRecord(**data)
