from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .models import MemoryRecord


class MemoryRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    value_signature TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(namespace, key)
                )
                """
            )

    def upsert(self, namespace: str, key: str, value: str, value_signature: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO memory_records(namespace, key, value, value_signature, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key)
                DO UPDATE SET
                    value = excluded.value,
                    value_signature = excluded.value_signature,
                    updated_at = excluded.updated_at
                """,
                (namespace, key, value, value_signature, now, now),
            )

    def get(self, namespace: str, key: str) -> MemoryRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, namespace, key, value, value_signature, created_at, updated_at
                FROM memory_records
                WHERE namespace = ? AND key = ?
                """,
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_model(row)

    def list_namespace(self, namespace: str) -> list[MemoryRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, namespace, key, value, value_signature, created_at, updated_at
                FROM memory_records
                WHERE namespace = ?
                ORDER BY key ASC
                """,
                (namespace,),
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            namespace=row["namespace"],
            key=row["key"],
            value=row["value"],
            value_signature=row["value_signature"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
