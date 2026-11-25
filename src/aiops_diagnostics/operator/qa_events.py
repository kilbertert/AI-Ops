"""问答 SSE 实时事件流持久化、进程内唤醒与白名单脱敏节点路径投影（#1709）。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.i18n import localized_pack
from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.gateway import gateway_db, gateway_schema
from aiops_diagnostics.operator.operator_agent import _SOURCE_LABELS

EVENT_RETENTION_DAYS = 7
SSE_HEARTBEAT_SECONDS = 15.0
TERMINAL_EVENT_TYPES = frozenset({"message_done", "run_cancelled", "run_failed"})

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_SQL_RE = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|UNION)\b.+", re.IGNORECASE)

_STEP_TITLES: dict[str, dict[str, str]] = {
    "zh": {
        "parse_attachments": "解析已上传附件上下文",
        "authorize_scope": "核验账号权限与场站范围",
        "dispatch_agent": "调度运维排查智能体",
        "answer_review": "复核结论与证据一致性",
        "generate_answer": "生成排查回复",
        "resample": "补充核验并重新组织答复",
        "tool_default": "读取只读业务证据",
    },
    "zh-Hant": {
        "parse_attachments": "解析已上傳附件上下文",
        "authorize_scope": "核驗帳號權限與站點範圍",
        "dispatch_agent": "調度營運排查智能體",
        "answer_review": "覆核結論與證據一致性",
        "generate_answer": "產生排查回覆",
        "resample": "補充核驗並重新組織答覆",
        "tool_default": "讀取唯讀業務證據",
    },
    "en": {
        "parse_attachments": "Parse uploaded attachments",
        "authorize_scope": "Verify account scope & stations",
        "dispatch_agent": "Dispatch operations assistant",
        "answer_review": "Verify conclusions against evidence",
        "generate_answer": "Generate troubleshooting reply",
        "resample": "Re-check and regenerate response",
        "tool_default": "Read authorized business evidence",
    },
}

_REPLACE_NOTICES: dict[str, dict[str, str]] = {
    "zh": {
        "evidence_only": "已切换为仅交付本轮核验通过的只读证据记录。",
        "reviewed": "已按终局复核结果更新最终回答内容。",
    },
    "zh-Hant": {
        "evidence_only": "已切換為僅交付本輪核驗通過的唯讀證據記錄。",
        "reviewed": "已按終局覆核結果更新最終回答內容。",
    },
    "en": {
        "evidence_only": "Switched to delivering verified read-only evidence records only.",
        "reviewed": "Updated final reply after consistency review.",
    },
}


def sanitize_step_summary(value: str | None) -> str | None:
    """严格脱敏节点摘要：剔除令牌、URL、SQL 与结构化参数片段，限制 <= 120 字符。"""
    if not value:
        return None
    cleaned = redact_text(str(value))
    cleaned = _BEARER_RE.sub("[REDACTED]", cleaned)
    cleaned = _URL_RE.sub("[REDACTED_URL]", cleaned)
    cleaned = _SQL_RE.sub("[REDACTED_QUERY]", cleaned)
    cleaned = cleaned.replace("{", "").replace("}", "").replace("\r", " ").replace("\n", " ")
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:120] if cleaned else None


def replace_notice(language: str, *, evidence_only: bool) -> str:
    pack = localized_pack(_REPLACE_NOTICES, language)
    return pack["evidence_only"] if evidence_only else pack["reviewed"]


def step_descriptor(phase: str, tool: str | None, language: str = "zh") -> tuple[str, str, str] | None:
    """白名单映射：将内部阶段/工具转为 ``(node_type, node_code, localized_title)``。"""
    titles = localized_pack(_STEP_TITLES, language)
    if phase == "attachment_parse":
        return ("attachment_parse", "parse_attachments", titles["parse_attachments"])
    if phase == "authorize":
        return ("route", "authorize_scope", titles["authorize_scope"])
    if phase == "dispatch":
        return ("route", "dispatch_agent", titles["dispatch_agent"])
    if phase == "resample":
        return ("route", "resample", titles["resample"])
    if phase == "tool" and tool:
        sources = localized_pack(_SOURCE_LABELS, language)
        title = sources.get(tool) or titles["tool_default"]
        return ("tool", tool, title)
    if phase == "review":
        return ("review", "answer_review", titles["answer_review"])
    if phase == "answer":
        return ("answer", "generate_answer", titles["generate_answer"])
    return None


def project_steps_from_trace(trace: list[dict[str, Any]], *, language: str = "zh") -> list[dict[str, Any]]:
    """从运行 trace 构造有序、白名单脱敏的 ``ExecutionStep[]`` 数组。"""
    steps: list[dict[str, Any]] = []
    open_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    counter = 0
    for item in trace or []:
        phase = str(item.get("phase") or "")
        if phase in {"completed", "failed", "cancelled", "expired"}:
            terminal_status = "succeeded" if phase == "completed" else "failed"
            elapsed_ms = max(0, int(item.get("elapsed_ms") or 0))
            for pending in open_by_key.values():
                pending["status"] = terminal_status
                if pending.get("duration_ms") is None:
                    pending["duration_ms"] = max(0, elapsed_ms - int(pending.get("elapsed_ms") or 0))
                pending["elapsed_ms"] = max(int(pending.get("elapsed_ms") or 0), elapsed_ms)
            open_by_key.clear()
            continue
        tool = item.get("tool")
        desc = step_descriptor(phase, str(tool) if tool else None, language)
        if desc is None:
            continue
        node_type, node_code, title = desc
        key = (node_type, node_code)
        raw_status = str(item.get("status") or "running")
        mapped_status = (
            "running"
            if raw_status == "running"
            else "succeeded"
            if raw_status in {"succeeded", "completed"}
            else "skipped"
            if raw_status == "skipped"
            else "failed"
        )
        elapsed_ms = int(item.get("elapsed_ms") or 0)
        duration_ms = item.get("duration_ms")
        summary = sanitize_step_summary(item.get("summary") or None)
        if mapped_status == "running" or key not in open_by_key:
            counter += 1
            step = {
                "step_id": f"st_{counter}",
                "node_type": node_type,
                "node_code": node_code,
                "title": title,
                "status": mapped_status,
                "elapsed_ms": max(0, elapsed_ms),
                "duration_ms": int(duration_ms) if duration_ms is not None else None,
                "input_summary": summary if mapped_status == "running" else None,
                "output_summary": summary if mapped_status != "running" else None,
            }
            steps.append(step)
            if mapped_status == "running":
                open_by_key[key] = step
        else:
            existing = open_by_key.pop(key)
            existing["status"] = mapped_status
            existing["elapsed_ms"] = max(0, elapsed_ms)
            if duration_ms is not None:
                existing["duration_ms"] = max(0, int(duration_ms))
            if summary:
                existing["output_summary"] = summary
    return steps


def format_sse_frame(event_type: str, payload: dict[str, Any], *, event_id: int | None = None) -> str:
    """按 W3C EventSource 格式编码单帧 SSE 事件。"""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


class QAEventStore:
    """短期有序问答事件流存储（支持断线续传与零轮询异步唤醒）。"""

    _CLEANUP_INTERVAL_SECONDS = 300.0

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        gateway_schema.ensure(self.path)
        protect_private_file(self.path)
        self._lock = threading.Lock()
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._last_cleanup_monotonic = 0.0

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with gateway_db.connection(self.path, write=write) as conn:
            yield conn

    @staticmethod
    def _retention_cutoff(now: datetime | None = None) -> str:
        return format_stored_time((now or now_utc()) - timedelta(days=EVENT_RETENTION_DAYS))

    def _maybe_expire(self, conn: sqlite3.Connection, now: datetime, *, force: bool = False) -> None:
        monotonic_now = time.monotonic()
        if not force and (monotonic_now - self._last_cleanup_monotonic) < self._CLEANUP_INTERVAL_SECONDS:
            return
        conn.execute(
            "DELETE FROM assistant_qa_events WHERE created_at <= ?",
            (self._retention_cutoff(now),),
        )
        self._last_cleanup_monotonic = monotonic_now

    def append(self, qa_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.append_many(qa_id, [(event_type, payload)])[0]

    def append_many(self, qa_id: str, events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """在单个写事务内按序写入多帧事件并统一唤醒订阅者（避免终局复核替换竞态）。"""
        if not events:
            return []
        now = now_utc()
        now_text = format_stored_time(now)
        force_cleanup = any(et == "run_started" for et, _ in events)
        created: list[dict[str, Any]] = []
        with self._connection(write=True) as conn:
            self._maybe_expire(conn, now, force=force_cleanup)
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM assistant_qa_events WHERE qa_id = ?",
                (qa_id,),
            ).fetchone()
            next_seq = int(row["max_seq"])
            for event_type, payload in events:
                next_seq += 1
                body = {"seq": next_seq, **payload}
                conn.execute(
                    """
                    INSERT INTO assistant_qa_events (qa_id, seq, event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        qa_id,
                        next_seq,
                        event_type,
                        json.dumps(body, ensure_ascii=False),
                        now_text,
                    ),
                )
                created.append({"seq": next_seq, "event": event_type, "data": body})
        self._notify(qa_id)
        return created

    def list_after(self, qa_id: str, after_seq: int = 0, *, limit: int = 500) -> list[dict[str, Any]]:
        cutoff = self._retention_cutoff()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT seq, event_type, payload_json
                FROM assistant_qa_events
                WHERE qa_id = ? AND seq > ? AND created_at > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (qa_id, max(0, int(after_seq)), cutoff, limit),
            ).fetchall()
        return [
            {
                "seq": int(r["seq"]),
                "event": str(r["event_type"]),
                "data": json.loads(str(r["payload_json"])),
            }
            for r in rows
        ]

    def has_any_events(self, qa_id: str) -> bool:
        cutoff = self._retention_cutoff()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM assistant_qa_events WHERE qa_id = ? AND created_at > ? LIMIT 1",
                (qa_id, cutoff),
            ).fetchone()
        return row is not None

    def has_delta_events(self, qa_id: str) -> bool:
        cutoff = self._retention_cutoff()
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM assistant_qa_events
                WHERE qa_id = ? AND event_type = 'message_delta' AND created_at > ?
                LIMIT 1
                """,
                (qa_id, cutoff),
            ).fetchone()
        return row is not None

    def streamed_text(self, qa_id: str) -> str:
        cutoff = self._retention_cutoff()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM assistant_qa_events
                WHERE qa_id = ? AND event_type = 'message_delta' AND created_at > ?
                ORDER BY seq ASC
                """,
                (qa_id, cutoff),
            ).fetchall()
        return "".join(str(json.loads(str(r["payload_json"])).get("delta") or "") for r in rows)

    def has_terminal_event(self, qa_id: str) -> bool:
        placeholders = ",".join("?" for _ in TERMINAL_EVENT_TYPES)
        cutoff = self._retention_cutoff()
        with self._connection() as conn:
            row = conn.execute(
                f"""
                SELECT 1 FROM assistant_qa_events
                WHERE qa_id = ? AND event_type IN ({placeholders}) AND created_at > ?
                LIMIT 1
                """,
                (qa_id, *sorted(TERMINAL_EVENT_TYPES), cutoff),
            ).fetchone()
        return row is not None

    def expire_now(self, *, at: datetime | None = None) -> int:
        cutoff = self._retention_cutoff(at)
        with self._connection(write=True) as conn:
            cursor = conn.execute(
                "DELETE FROM assistant_qa_events WHERE created_at <= ?",
                (cutoff,),
            )
            return cursor.rowcount

    async def wait_for_update(self, qa_id: str, *, timeout: float = SSE_HEARTBEAT_SECONDS) -> bool:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        entry = (loop, event)
        with self._lock:
            self._waiters.setdefault(qa_id, []).append(entry)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            with self._lock:
                listeners = self._waiters.get(qa_id)
                if listeners and entry in listeners:
                    listeners.remove(entry)
                if listeners is not None and not listeners:
                    self._waiters.pop(qa_id, None)

    def _notify(self, qa_id: str) -> None:
        with self._lock:
            listeners = list(self._waiters.get(qa_id, ()))
        for loop, event in listeners:
            if not loop.is_closed():
                loop.call_soon_threadsafe(event.set)
