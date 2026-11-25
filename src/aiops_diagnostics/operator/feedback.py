"""问答轮次点赞/点踩反馈持久化存储（#1712）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.gateway import gateway_db, gateway_schema


def _feedback_from_row(row: sqlite3.Row) -> dict[str, Any]:
    raw_reasons = row["reasons_json"]
    reasons = json.loads(raw_reasons) if raw_reasons else []
    return {
        "qa_id": str(row["qa_id"]),
        "rating": row["rating"],
        "reasons": list(reasons),
        "comment": row["comment"],
        "updated_at": str(row["updated_at"]),
    }


class AssistantFeedbackStore:
    """按 `(qa_id, scope_fingerprint)` 幂等保存与查询回答反馈。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        gateway_schema.ensure(self.path)

    def _connection(self, *, write: bool = False):
        return gateway_db.connection(self.path, write=write)

    def upsert(
        self,
        qa_id: str,
        scope_fingerprint: str,
        *,
        rating: str | None,
        reasons: list[str] | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        now = format_stored_time(now_utc())
        if rating is None:
            clean_reasons: list[str] = []
            clean_comment: str | None = None
        else:
            clean_reasons = [
                redact_text(str(item)).strip()[:64] for item in (reasons or []) if str(item).strip()
            ]
            raw_comment = (comment or "").strip()
            clean_comment = redact_text(raw_comment)[:500] if raw_comment else None
        reasons_json = json.dumps(clean_reasons, ensure_ascii=False)
        with self._connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO assistant_feedback (
                    qa_id, scope_fingerprint, rating, reasons_json, comment, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qa_id) DO UPDATE SET
                    rating = excluded.rating,
                    reasons_json = excluded.reasons_json,
                    comment = excluded.comment,
                    updated_at = excluded.updated_at
                WHERE assistant_feedback.scope_fingerprint = excluded.scope_fingerprint
                """,
                (qa_id, scope_fingerprint, rating, reasons_json, clean_comment, now, now),
            )
            row = conn.execute(
                "SELECT * FROM assistant_feedback WHERE qa_id = ? AND scope_fingerprint = ?",
                (qa_id, scope_fingerprint),
            ).fetchone()
        assert row is not None
        return _feedback_from_row(row)

    def get(self, qa_id: str, scope_fingerprint: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_feedback WHERE qa_id = ? AND scope_fingerprint = ?",
                (qa_id, scope_fingerprint),
            ).fetchone()
        return _feedback_from_row(row) if row is not None else None

    def get_many(self, qa_ids: list[str], scope_fingerprint: str) -> dict[str, dict[str, Any]]:
        cleaned = [str(qid) for qid in qa_ids if qid]
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM assistant_feedback WHERE scope_fingerprint = ? AND qa_id IN ({placeholders})",
                (scope_fingerprint, *cleaned),
            ).fetchall()
        return {str(row["qa_id"]): _feedback_from_row(row) for row in rows}
