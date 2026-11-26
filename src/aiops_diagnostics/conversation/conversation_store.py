"""Conversation and active-order context (T4/#172).

A conversation binds one user's assistant thread to its scope (caller,
subject, tenant), business entry, and serving agent version, and remembers the
order the user confirmed as ``active_order``.  The unified assistant entry
point uses the conversation to (a) let follow-up order questions omit the
order number, and (b) keep a bounded turn history — the most recent 8 turns
or 8k tokens, whichever is smaller, retained for 30 days.

Every turn re-verifies scope, entry, agent, and order authorization against
the CURRENT request; stored context never substitutes for a fresh check.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc, parse_stored_time
from aiops_diagnostics.gateway import gateway_db, gateway_schema
from aiops_diagnostics.identity.order_identity import is_order_identifier

CONVERSATION_RETENTION_DAYS = 30
CONTEXT_MAX_TURNS = 8
CONTEXT_MAX_TOKENS = 8000
BUSY_LOCK_SECONDS = 120  # a crashed worker's generating flag self-expires
AUTO_TITLE_MAX_CHARS = 40
TITLE_MAX_CHARS = 80


def auto_conversation_title(question: str) -> str:
    """基于首轮提问生成清洗截断后的简短标题（<= 40 字符）。"""
    cleaned = " ".join(redact_text(question or "").split()).strip()
    if len(cleaned) <= AUTO_TITLE_MAX_CHARS:
        return cleaned
    return cleaned[: AUTO_TITLE_MAX_CHARS - 1].rstrip() + "…"


_SAFE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ENTRY = re.compile(r"^(consumer|operator)$")
_QUESTION_MAX = 4000


class ConversationError(RuntimeError):
    """Raised for invalid conversation state transitions or arguments."""


class ConversationNotFound(ConversationError):
    """The conversation does not exist for this scope (unified not-found)."""


class ConversationBusy(ConversationError):
    """The conversation already has a generating turn."""


class RegenerateNotAllowed(ConversationError):
    """Only the latest completed turn of a conversation can be regenerated."""


@dataclass(frozen=True, slots=True)
class TurnCompletionMetadata:
    """单轮问答落库与释放生成槽位时的附加元数据。"""

    token_count: int = 0
    status_qa_id: str | None = None
    active_order_no: str | None = None
    attempt_no: int | None = None
    parent_qa_id: str | None = None
    attachment_ids: Iterable[str] | None = None
    citations: Iterable[dict[str, Any]] | None = None
    steps: Iterable[dict[str, Any]] | None = None
    elapsed_ms: int | None = None
    delivery_mode: str | None = None
    preserve_completed: bool = False
    claim_id: str | None = None


class ConversationStore:
    """SQLite persistence for conversations and their bounded turn history.

    Shares the gateway database file (its own tables) so one backup covers
    both.  All lookups are scope_fingerprint-scoped: another user's
    conversation is indistinguishable from a missing one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        gateway_schema.ensure(self.path)
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        # 连接与锁纪律唯一渲染在 gateway_db（#573）。本 store 补上了
        # foreign_keys=ON —— 曾经少了这一条（同一文件上没有「不强制
        # 外键」这个选项）。
        with gateway_db.connection(self.path, write=write) as connection:
            yield connection

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        scope_fingerprint: str,
        business_entry: str,
        agent_version_key: str,
        operator_binding: str = "",
        agent_id: str = "operator_main",
        title: str = "",
    ) -> dict[str, Any]:
        scope = self._validate_scope(scope_fingerprint)
        if not _SAFE_ENTRY.fullmatch(business_entry or ""):
            raise ConversationError("business entry is invalid")
        agent_version_key = self._validate_agent_version(agent_version_key)
        resolved_agent_id = (agent_id or "operator_main").strip() or "operator_main"
        resolved_title = " ".join((title or "").split()).strip()[:TITLE_MAX_CHARS]
        now = now_utc()
        conversation_id = "conv_" + uuid.uuid4().hex
        expires_at = now + timedelta(days=CONVERSATION_RETENTION_DAYS)
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, scope_fingerprint, business_entry,
                    agent_version_key, active_order_no, generating_since,
                    created_at, updated_at, expires_at, operator_binding, agent_id, title
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    scope,
                    business_entry,
                    agent_version_key,
                    format_stored_time(now),
                    format_stored_time(now),
                    format_stored_time(expires_at),
                    operator_binding,
                    resolved_agent_id,
                    resolved_title,
                ),
            )
        return self.get(conversation_id, scope_fingerprint)

    def get(self, conversation_id: str, scope_fingerprint: str) -> dict[str, Any]:
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now_utc())
            row = connection.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM conversation_turns t
                        WHERE t.conversation_id = c.conversation_id) AS turn_count
                FROM conversations c
                WHERE c.conversation_id = ? AND c.scope_fingerprint = ?
                """,
                (conversation_id, scope),
            ).fetchone()
        if row is None:
            raise ConversationNotFound("conversation not found")
        return _conversation_from_row(row)

    def list(
        self,
        scope_fingerprint: str,
        *,
        limit: int = 50,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope = self._validate_scope(scope_fingerprint)
        if not 1 <= limit <= 200:
            raise ConversationError("limit must be between 1 and 200")
        clauses = ["c.scope_fingerprint = ?", "c.operator_binding NOT LIKE '%:preview'"]
        params: list[Any] = [scope]
        if agent_id is not None and agent_id.strip():
            clauses.append("c.agent_id = ?")
            params.append(agent_id.strip())
        params.append(limit)
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now_utc())
            rows = connection.execute(
                f"""
                SELECT c.*,
                       (SELECT COUNT(*) FROM conversation_turns t
                        WHERE t.conversation_id = c.conversation_id) AS turn_count
                FROM conversations c
                WHERE {" AND ".join(clauses)}
                ORDER BY c.updated_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def rename(self, conversation_id: str, scope_fingerprint: str, title: str) -> dict[str, Any]:
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        cleaned = " ".join(redact_text(title or "").split()).strip()
        if not cleaned or len(cleaned) > TITLE_MAX_CHARS:
            raise ConversationError("conversation title is invalid")
        now = now_utc()
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            updated = connection.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE conversation_id = ? AND scope_fingerprint = ?
                """,
                (cleaned, format_stored_time(now), conversation_id, scope),
            ).rowcount
            if not updated:
                raise ConversationNotFound("conversation not found")
        return self.get(conversation_id, scope_fingerprint)

    def delete(self, conversation_id: str, scope_fingerprint: str) -> None:
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now_utc())
            deleted = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ? AND scope_fingerprint = ?",
                (conversation_id, scope),
            ).rowcount
            if deleted:
                connection.execute(
                    "DELETE FROM conversation_turns WHERE conversation_id = ?",
                    (conversation_id,),
                )
        # A missing conversation deletes as a no-op — same observable result.

    # ------------------------------------------------------------------
    # active order + agent version
    # ------------------------------------------------------------------

    def set_active_order(
        self, conversation_id: str, scope_fingerprint: str, order_no: str | None
    ) -> dict[str, Any]:
        """Bind or clear the conversation's confirmed order.

        ``None``/"" clears it. The CALLER must verify order ownership before
        binding — this store only persists the decision, never re-derives it.
        """
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        if order_no is not None and not is_order_identifier(order_no):
            raise ConversationError("order number is invalid")
        now = now_utc()
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            updated = connection.execute(
                """
                UPDATE conversations
                SET active_order_no = ?, updated_at = ?, expires_at = ?
                WHERE conversation_id = ? AND scope_fingerprint = ?
                """,
                (
                    order_no,
                    format_stored_time(now),
                    format_stored_time(now + timedelta(days=CONVERSATION_RETENTION_DAYS)),
                    conversation_id,
                    scope,
                ),
            ).rowcount
            if not updated:
                raise ConversationNotFound("conversation not found")
        return self.get(conversation_id, scope_fingerprint)

    def set_agent_version(
        self, conversation_id: str, scope_fingerprint: str, agent_version_key: str
    ) -> dict[str, Any]:
        """Point new turns at a different agent version (used when the
        serving agent is republished; in-flight turns keep their snapshot)."""
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        agent_version_key = self._validate_agent_version(agent_version_key)
        now = now_utc()
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            updated = connection.execute(
                """
                UPDATE conversations
                SET agent_version_key = ?, updated_at = ?
                WHERE conversation_id = ? AND scope_fingerprint = ?
                """,
                (agent_version_key, format_stored_time(now), conversation_id, scope),
            ).rowcount
            if not updated:
                raise ConversationNotFound("conversation not found")
        return self.get(conversation_id, scope_fingerprint)

    # ------------------------------------------------------------------
    # generation lock (409 CONVERSATION_BUSY)
    # ------------------------------------------------------------------

    def begin_turn(
        self,
        conversation_id: str,
        scope_fingerprint: str,
        *,
        kind: str,
        question: str,
        attachment_ids: list[str] | None = None,
        claim_id: str | None = None,
    ) -> int:
        """Claim the conversation's generation slot; returns the new turn_no.

        Raises ConversationBusy when a live generation holds the slot. The
        claim self-expires after BUSY_LOCK_SECONDS so a crashed worker cannot
        wedge the conversation forever — which is also why the claim records
        WHICH turn holds it (``generating_turn_no``): a worker that outlived
        its own claim must not release the slot of the turn that replaced it.

        Turn numbers are never reused. A stopped turn's row is deleted, so a
        number handed out once has to stay spent — otherwise the next turn
        would inherit the stopped turn's identity, and a worker finishing late
        would write to (and unlock) somebody else's turn.
        """
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        if kind not in {"qa", "diagnosis", "faq"}:
            raise ConversationError("turn kind is invalid")
        question = self._validate_question(question)
        now = now_utc()
        lock_expiry = now - timedelta(seconds=BUSY_LOCK_SECONDS)
        attachments_payload = json.dumps(list(attachment_ids), ensure_ascii=False) if attachment_ids else None
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            row = connection.execute(
                """
                SELECT generating_since, last_turn_no, title FROM conversations
                WHERE conversation_id = ? AND scope_fingerprint = ?
                """,
                (conversation_id, scope),
            ).fetchone()
            if row is None:
                raise ConversationNotFound("conversation not found")
            generating_since = parse_stored_time(row["generating_since"]) if row["generating_since"] else None
            if generating_since is not None and generating_since > lock_expiry:
                raise ConversationBusy("conversation already has a generating turn")
            highest_row = connection.execute(
                """
                SELECT COALESCE(MAX(turn_no), 0) + 1 AS next
                FROM conversation_turns WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()["next"]
            # A stopped turn's row is gone, so the rows alone can undercount:
            # the spent number is the floor.
            next_no = max(int(row["last_turn_no"] or 0) + 1, int(highest_row))
            connection.execute(
                """
                INSERT INTO conversation_turns
                    (turn_id, conversation_id, turn_no, kind, question, token_count,
                     created_at, attachment_ids_json)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    "turn_" + uuid.uuid4().hex,
                    conversation_id,
                    next_no,
                    kind,
                    question,
                    format_stored_time(now),
                    attachments_payload,
                ),
            )
            new_title = str(row["title"] or "").strip() or auto_conversation_title(question)
            active_claim = claim_id or ("clm_" + uuid.uuid4().hex)
            connection.execute(
                """
                UPDATE conversations
                SET generating_since = ?, generating_turn_no = ?, generating_claim_id = ?,
                    last_turn_no = ?, title = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (
                    format_stored_time(now),
                    next_no,
                    active_claim,
                    next_no,
                    new_title,
                    format_stored_time(now),
                    conversation_id,
                ),
            )
            return int(next_no)

    def find_turn_by_qa_id(self, qa_id: str, scope_fingerprint: str) -> dict[str, Any] | None:
        """按 ``status_qa_id`` 反查所属会话与轮次记录（强制按主体隔离）。"""
        scope = self._validate_scope(scope_fingerprint)
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now_utc())
            row = connection.execute(
                """
                SELECT t.*, c.business_entry, c.agent_id, c.active_order_no
                FROM conversation_turns t
                JOIN conversations c USING(conversation_id)
                WHERE t.status_qa_id = ? AND c.scope_fingerprint = ?
                LIMIT 1
                """,
                (qa_id, scope),
            ).fetchone()
        if row is None:
            return None
        turn = _turn_from_row(row)
        turn["business_entry"] = str(row["business_entry"])
        turn["agent_id"] = str(row["agent_id"] or "operator_main")
        turn["active_order_no"] = row["active_order_no"]
        return turn

    def begin_regenerate_turn(
        self,
        conversation_id: str,
        scope_fingerprint: str,
        *,
        expected_qa_id: str,
    ) -> dict[str, Any]:
        """原子校验“仅最后一轮可原地重生成”并占用生成槽位（不新增重复轮次）。"""
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        now = now_utc()
        lock_expiry = now - timedelta(seconds=BUSY_LOCK_SECONDS)
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            conv = connection.execute(
                """
                SELECT generating_since, business_entry, agent_id, active_order_no
                FROM conversations
                WHERE conversation_id = ? AND scope_fingerprint = ?
                """,
                (conversation_id, scope),
            ).fetchone()
            if conv is None:
                raise ConversationNotFound("conversation not found")
            generating_since = (
                parse_stored_time(conv["generating_since"]) if conv["generating_since"] else None
            )
            if generating_since is not None and generating_since > lock_expiry:
                raise ConversationBusy("conversation already has a generating turn")
            latest = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE conversation_id = ?
                ORDER BY turn_no DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if latest is None or latest["status_qa_id"] != expected_qa_id or latest["answer_json"] is None:
                raise RegenerateNotAllowed("only the latest completed turn can be regenerated")
            turn_no = int(latest["turn_no"])
            next_attempt_no = int(latest["attempt_no"] or 1) + 1
            raw_attachments = latest["attachment_ids_json"]
            attachment_ids = json.loads(raw_attachments) if raw_attachments else []
            claim_id = "clm_" + uuid.uuid4().hex
            connection.execute(
                """
                UPDATE conversations
                SET generating_since = ?, generating_turn_no = ?, generating_claim_id = ?, updated_at = ?
                WHERE conversation_id = ? AND scope_fingerprint = ?
                """,
                (
                    format_stored_time(now),
                    turn_no,
                    claim_id,
                    format_stored_time(now),
                    conversation_id,
                    scope,
                ),
            )
            return {
                "conversation_id": conversation_id,
                "turn_no": turn_no,
                "claim_id": claim_id,
                "next_attempt_no": next_attempt_no,
                "question": str(latest["question"]),
                "attachment_ids": attachment_ids,
                "agent_id": str(conv["agent_id"] or "operator_main"),
                "active_order_no": conv["active_order_no"],
                "business_entry": str(conv["business_entry"]),
            }

    def complete_turn(
        self,
        conversation_id: str,
        scope_fingerprint: str,
        turn_no: int,
        *,
        answer: dict[str, Any] | None,
        metadata: TurnCompletionMetadata | None = None,
        token_count: int = 0,
        cancelled: bool = False,
        status_qa_id: str | None = None,
        active_order_no: str | None = None,
        attempt_no: int | None = None,
        parent_qa_id: str | None = None,
        attachment_ids: list[str] | None = None,
        citations: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
        elapsed_ms: int | None = None,
        delivery_mode: str | None = None,
        preserve_completed: bool = False,
        claim_id: str | None = None,
    ) -> None:
        """Persist a finished turn and release the generation slot.

        A cancelled turn keeps its question row (audit) but never stores an
        answer — an interrupted generation must not survive as a complete one.

        The slot is released only while THIS claim still holds it. For in-place
        regeneration (which reuses ``turn_no``), ``claim_id`` uniquely identifies
        each generation attempt so a stale worker finishing late can neither
        overwrite a newer attempt's answer nor unlock a newer attempt's slot.
        """
        meta = metadata or TurnCompletionMetadata(
            token_count=token_count,
            status_qa_id=status_qa_id,
            active_order_no=active_order_no,
            attempt_no=attempt_no,
            parent_qa_id=parent_qa_id,
            attachment_ids=attachment_ids,
            citations=citations,
            steps=steps,
            elapsed_ms=elapsed_ms,
            delivery_mode=delivery_mode,
            preserve_completed=preserve_completed,
            claim_id=claim_id,
        )
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        if meta.status_qa_id is not None and not re.fullmatch(r"qa_[a-f0-9]{32}", meta.status_qa_id):
            raise ConversationError("status question id is invalid")
        if meta.parent_qa_id is not None and not re.fullmatch(r"qa_[a-f0-9]{32}", meta.parent_qa_id):
            raise ConversationError("parent question id is invalid")
        if meta.active_order_no is not None and not is_order_identifier(meta.active_order_no):
            raise ConversationError("order number is invalid")
        now = now_utc()
        with self._connection(write=True) as connection:
            self._expire_conversations(connection, now)
            row = connection.execute(
                "SELECT turn_id FROM conversation_turns WHERE conversation_id = ? AND turn_no = ?",
                (conversation_id, turn_no),
            ).fetchone()
            if row is None:
                raise ConversationNotFound("turn not found")
            if meta.claim_id is not None:
                holder = connection.execute(
                    """
                    SELECT generating_turn_no, generating_claim_id FROM conversations
                    WHERE conversation_id = ? AND scope_fingerprint = ?
                    """,
                    (conversation_id, scope),
                ).fetchone()
                holds_slot = (
                    holder is not None
                    and holder["generating_turn_no"] == turn_no
                    and holder["generating_claim_id"] == meta.claim_id
                )
                if not holds_slot and (meta.preserve_completed or cancelled):
                    return
            if cancelled:
                if meta.preserve_completed:
                    # 原地重生成失败/取消时保留原已完成答复，仅释放槽位。
                    connection.execute(
                        "DELETE FROM conversation_turns WHERE conversation_id = ? AND turn_no = ? "
                        "AND answer_json IS NULL",
                        (conversation_id, turn_no),
                    )
                else:
                    connection.execute(
                        "DELETE FROM conversation_turns WHERE conversation_id = ? AND turn_no = ?",
                        (conversation_id, turn_no),
                    )
            else:
                connection.execute(
                    """
                    UPDATE conversation_turns
                    SET answer_json = ?,
                        token_count = ?,
                        created_at = ?,
                        status_qa_id = ?,
                        attempt_no = COALESCE(?, attempt_no),
                        parent_qa_id = COALESCE(?, parent_qa_id),
                        attachment_ids_json = COALESCE(?, attachment_ids_json),
                        citations_json = COALESCE(?, citations_json),
                        steps_json = COALESCE(?, steps_json),
                        elapsed_ms = COALESCE(?, elapsed_ms),
                        delivery_mode = COALESCE(?, delivery_mode)
                    WHERE conversation_id = ? AND turn_no = ?
                    """,
                    (
                        json.dumps(answer, ensure_ascii=False) if answer else None,
                        max(0, int(meta.token_count)),
                        format_stored_time(now),
                        meta.status_qa_id,
                        int(meta.attempt_no) if meta.attempt_no is not None else None,
                        meta.parent_qa_id,
                        json.dumps(list(meta.attachment_ids), ensure_ascii=False)
                        if meta.attachment_ids is not None
                        else None,
                        json.dumps(list(meta.citations), ensure_ascii=False)
                        if meta.citations is not None
                        else None,
                        json.dumps(list(meta.steps), ensure_ascii=False) if meta.steps is not None else None,
                        max(0, int(meta.elapsed_ms)) if meta.elapsed_ms is not None else None,
                        meta.delivery_mode,
                        conversation_id,
                        turn_no,
                    ),
                )
            claim_clause = " AND generating_claim_id = ?" if meta.claim_id is not None else ""
            claim_args: tuple[Any, ...] = (meta.claim_id,) if meta.claim_id is not None else ()
            if meta.active_order_no is not None and not cancelled:
                connection.execute(
                    "UPDATE conversations SET active_order_no=? WHERE conversation_id=? "
                    f"AND scope_fingerprint=? AND generating_turn_no=?{claim_clause}",
                    (meta.active_order_no, conversation_id, scope, turn_no, *claim_args),
                )
            connection.execute(
                f"""
                UPDATE conversations
                SET generating_since = NULL, generating_turn_no = NULL, generating_claim_id = NULL,
                    updated_at = ?, expires_at = MAX(expires_at, ?)
                WHERE conversation_id = ? AND scope_fingerprint = ? AND generating_turn_no = ?{claim_clause}
                """,
                (
                    format_stored_time(now),
                    format_stored_time(now + timedelta(days=CONVERSATION_RETENTION_DAYS)),
                    conversation_id,
                    scope,
                    turn_no,
                    *claim_args,
                ),
            )

    def renew_turn_claim(
        self,
        conversation_id: str,
        scope_fingerprint: str,
        turn_no: int,
        *,
        claim_id: str | None = None,
    ) -> bool:
        """Push this turn's claim back to now, if it still holds the slot."""
        self._validate_conversation_id(conversation_id)
        scope = self._validate_scope(scope_fingerprint)
        if not isinstance(turn_no, int) or turn_no < 1:
            raise ConversationError("turn number is invalid")
        claim_clause = " AND generating_claim_id = ?" if claim_id is not None else ""
        claim_args: tuple[Any, ...] = (claim_id,) if claim_id is not None else ()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE conversations SET generating_since = ?"
                " WHERE conversation_id = ? AND scope_fingerprint = ?"
                f" AND generating_turn_no = ?{claim_clause}",
                (format_stored_time(now_utc()), conversation_id, scope, turn_no, *claim_args),
            )
            return cursor.rowcount > 0

    def recover_interrupted_turns(self) -> int:
        """Free the generation slots a dead process left held (T6/#492).

        A slot is held until its worker completes the turn or
        ``BUSY_LOCK_SECONDS`` lapses. When a restart killed the worker, nothing
        completes it — so a caller cannot ask again in that conversation until
        the crash fallback expires, up to two minutes after a deploy. That is a
        latency defect, not a correctness one: the fallback does eventually
        release the slot. It is still the difference between "retry now" and
        "retry in two minutes" for someone whose generation was killed.

        Runs on the boot path, before the app serves anything, so a newer turn
        cannot exist yet and there is nothing to protect with a turn-number
        match — the whole point of that match is to avoid clearing a slot a
        LATER worker took, and at boot there is no later worker.

        The turn row goes with the slot, and only the one the slot names: an
        interrupted generation must not survive as a question with no answer
        (#172), which is the same treatment a failed turn gets from its worker.
        Deleting by ``generating_turn_no`` rather than by "every answer-less
        row" keeps a turn that some other exit already closed out of this one's
        way.

        Returns how many slots were freed.
        """
        with self._connection(write=True) as connection:
            held = [
                (str(row["conversation_id"]), int(row["generating_turn_no"]))
                for row in connection.execute(
                    "SELECT conversation_id, generating_turn_no FROM conversations"
                    " WHERE generating_since IS NOT NULL AND generating_turn_no IS NOT NULL"
                ).fetchall()
            ]
            for conversation_id, turn_no in held:
                connection.execute(
                    "DELETE FROM conversation_turns WHERE conversation_id = ? AND turn_no = ? "
                    "AND answer_json IS NULL",
                    (conversation_id, turn_no),
                )
            cursor = connection.execute(
                "UPDATE conversations SET generating_since = NULL, generating_turn_no = NULL,"
                " generating_claim_id = NULL WHERE generating_since IS NOT NULL"
            )
            return cursor.rowcount

    def release_turn(
        self,
        conversation_id: str,
        scope_fingerprint: str,
        turn_no: int,
        *,
        preserve_completed: bool = False,
        claim_id: str | None = None,
    ) -> None:
        """Drop an unfinished/cancelled turn row and release the slot."""
        self.complete_turn(
            conversation_id,
            scope_fingerprint,
            turn_no,
            answer=None,
            cancelled=True,
            metadata=TurnCompletionMetadata(preserve_completed=preserve_completed, claim_id=claim_id),
        )

    # ------------------------------------------------------------------
    # bounded history
    # ------------------------------------------------------------------

    def context_turns(
        self,
        conversation_id: str,
        scope_fingerprint: str,
        *,
        max_turns: int = CONTEXT_MAX_TURNS,
        max_tokens: int = CONTEXT_MAX_TOKENS,
    ) -> list[dict[str, Any]]:
        """The most recent turns within BOTH limits (smaller window wins).

        Returns oldest-first for prompt assembly. Turns without a stored
        answer (interrupted/failed generations) never enter the window.
        Status-query turns are also excluded: their snapshots require a fresh
        authorization check and must not become current facts in a later answer.

        **"Recent" is a fact about the conversation, not a permission check**
        (#497). This selects by ``turn_no`` alone: it does not consult whether
        the order a turn was about is still visible to the caller. A turn whose
        order has left the caller's site set therefore stays in the window even
        though the binding has already been dropped by route 1c. Closing that
        gap needs a turn → order edge the table does not have (see the schema
        comment). Do not read a turn's presence here as evidence it is still
        authorised.

        The budget is not a hard ceiling on the returned turns: the newest turn
        is always kept, even alone, because an empty context is worse than one
        oversized turn. Callers that need a strict bound must say so.
        """
        self._validate_conversation_id(conversation_id)
        self._validate_scope(scope_fingerprint)  # ownership enforced by caller
        if not 1 <= max_turns <= 50 or not 100 <= max_tokens <= 100000:
            raise ConversationError("context limits are out of range")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE conversation_id = ? AND answer_json IS NOT NULL AND status_qa_id IS NULL
                ORDER BY turn_no DESC LIMIT ?
                """,
                (conversation_id, max_turns),
            ).fetchall()
        selected: list[dict[str, Any]] = []
        budget = max_tokens
        for row in rows:  # newest → oldest; stop once either limit is hit
            payload = _turn_from_row(row)
            cost = max(int(payload["token_count"] or 0), len(payload["question"]) // 2)
            if not selected:
                # Always keep the newest turn — even alone it can exceed the
                # budget (a single oversized turn never yields an empty
                # context, just that one turn).
                selected.append(payload)
                continue
            if cost > budget:
                break
            selected.append(payload)
            budget -= cost
        selected.reverse()
        return selected

    def previous_completed_qa(self, conversation_id, scope_fingerprint, before):
        self.get(conversation_id, scope_fingerprint)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status_qa_id FROM conversation_turns WHERE conversation_id=? "
                "AND turn_no<? AND answer_json IS NOT NULL ORDER BY turn_no DESC LIMIT 1",
                (conversation_id, before),
            ).fetchone()
        return row[0] if row else None

    def completed_context(self, conversation_id, scope_fingerprint, *, before, limit=6):
        """仅同一主体的已完成回合；业务对象权限由使用方再核对。"""
        self.get(conversation_id, scope_fingerprint)
        if not 1 <= limit <= 10 or before < 1:
            raise ConversationError("invalid context window")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT t.* FROM conversation_turns t JOIN conversations c USING(conversation_id) "
                "WHERE t.conversation_id=? AND c.scope_fingerprint=? AND t.turn_no<? "
                "AND t.answer_json IS NOT NULL ORDER BY t.turn_no DESC LIMIT ?",
                (conversation_id, scope_fingerprint, before, limit),
            ).fetchall()
        return [_turn_from_row(row) for row in reversed(rows)]

    def recent_questions(
        self, conversation_id: str, scope_fingerprint: str, *, before: int, limit: int = 3
    ) -> list[str]:
        """Completed questions only, bounded from the newest end and owner checked."""
        self.get(conversation_id, scope_fingerprint)
        if not 1 <= limit <= CONTEXT_MAX_TURNS or before < 1:
            raise ConversationError("invalid question window")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT t.question FROM conversation_turns t JOIN conversations c USING(conversation_id) "
                "WHERE t.conversation_id=? AND c.scope_fingerprint=? AND t.turn_no<? "
                "AND t.answer_json IS NOT NULL ORDER BY t.turn_no DESC LIMIT ?",
                (conversation_id, scope_fingerprint, before, limit),
            ).fetchall()
        return [str(row[0]) for row in reversed(rows)]

    def turns(
        self, conversation_id: str, scope_fingerprint: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Full turn history for the conversation detail view (all turns)."""
        self._validate_conversation_id(conversation_id)
        self._validate_scope(scope_fingerprint)  # ownership enforced by caller
        if not 1 <= limit <= 500:
            raise ConversationError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_turns WHERE conversation_id = ? ORDER BY turn_no ASC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [_turn_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_scope(value: str) -> str:
        candidate = (value or "").strip()
        if not _SAFE_SCOPE.fullmatch(candidate):
            raise ConversationError("scope fingerprint is invalid")
        return candidate

    @staticmethod
    def _validate_conversation_id(value: str) -> str:
        candidate = (value or "").strip()
        if not candidate.startswith("conv_") or len(candidate) > 64:
            raise ConversationError("conversation id is invalid")
        return candidate

    @staticmethod
    def _validate_agent_version(value: str) -> str:
        candidate = (value or "").strip()
        # agent_version_key comes from AgentVersion fields: agt_<hex>#v<int>
        if not re.fullmatch(r"agt_[A-Za-z0-9]{8,64}#v\d{1,6}", candidate):
            raise ConversationError("agent version key is invalid")
        return candidate

    @staticmethod
    def _validate_question(value: str) -> str:
        candidate = (value or "").strip()
        if not candidate or len(candidate) > _QUESTION_MAX:
            raise ConversationError("question is empty or too long")
        return redact_text(candidate)

    @staticmethod
    def _expire_conversations(connection: sqlite3.Connection, now: datetime) -> None:
        stamp = format_stored_time(now)
        connection.execute(
            "DELETE FROM conversation_turns WHERE conversation_id IN"
            " (SELECT conversation_id FROM conversations WHERE expires_at <= ?)",
            (stamp,),
        )
        connection.execute("DELETE FROM conversations WHERE expires_at <= ?", (stamp,))


def _conversation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["is_generating"] = bool(result.pop("generating_since"))
    result["title"] = str(result.get("title") or "")
    result["agent_id"] = str(result.get("agent_id") or "operator_main")
    result["turn_count"] = int(result.get("turn_count") or 0)
    # Internal claim bookkeeping: never part of the conversation's shape.
    result.pop("generating_turn_no", None)
    result.pop("last_turn_no", None)
    return result


def _turn_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw = result.pop("answer_json", None)
    result["answer"] = json.loads(raw) if raw else None
    raw_attachments = result.pop("attachment_ids_json", None)
    result["attachment_ids"] = json.loads(raw_attachments) if raw_attachments else []
    raw_citations = result.pop("citations_json", None)
    result["citations"] = json.loads(raw_citations) if raw_citations else []
    raw_steps = result.pop("steps_json", None)
    result["steps"] = json.loads(raw_steps) if raw_steps else []
    result["attempt_no"] = int(result.get("attempt_no") or 1)
    result["qa_id"] = result.get("status_qa_id")
    return result
