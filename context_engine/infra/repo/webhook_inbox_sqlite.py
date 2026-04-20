from __future__ import annotations

import sqlite3
from pathlib import Path


class WebhookInboxSqliteRepository:
    """SQLite-backed webhook idempotency store."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / ".context-garden" / "webhook_inbox.sqlite"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_inbox (
                    idempotency_key TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    delivery_id TEXT,
                    ref TEXT,
                    commit_sha TEXT,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def try_claim(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        delivery_id: str | None,
        ref: str | None,
        commit_sha: str | None,
        payload_json: str,
    ) -> bool:
        try:
            with sqlite3.connect(self._path) as conn:
                conn.execute(
                    """
                    INSERT INTO webhook_inbox (
                        idempotency_key, workspace_id, delivery_id, ref, commit_sha, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, workspace_id, delivery_id, ref, commit_sha, payload_json),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def release(self, *, idempotency_key: str) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("DELETE FROM webhook_inbox WHERE idempotency_key = ?", (idempotency_key,))
            conn.commit()

