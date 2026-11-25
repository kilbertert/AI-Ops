"""排查结论单轮报告导出（纯 Python 生成 .docx / .md，不执行业务写操作）。"""

from __future__ import annotations

import io
import re
from typing import Any

from docx import Document

from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.common.scalar_rules import now_utc

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MD_CONTENT_TYPE = "text/markdown; charset=utf-8"

_READONLY_DISCLAIMER = (
    "声明：本报告由充电桩后台 AI 只读排查助手汇总生成，仅包含已授权只读查询证据与分析记录，"
    "未对订单、计费、设备或业务工单执行任何变更操作。"
)
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def _clean_text(text: str) -> str:
    redacted = redact_text(str(text or ""))
    # 防止任何内部网关或对象存储预签名地址写入导出文档
    return _URL_RE.sub("[URL_REDACTED]", redacted).strip()


def _extract_answer_text(qa: dict[str, Any]) -> str:
    result = qa.get("result") or {}
    blocks = result.get("blocks") or []
    parts = [
        _clean_text(str(block.get("text") or ""))
        for block in blocks
        if isinstance(block, dict) and block.get("kind") == "text" and block.get("text")
    ]
    return "\n\n".join(part for part in parts if part)


def _safe_short_id(qa_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(qa_id or ""))
    return cleaned[:20] or "report"


def render_diagnosis_report(
    *,
    qa: dict[str, Any],
    fmt: str = "docx",
    steps: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> tuple[str, str, bytes]:
    """渲染单轮排查报告，返回 ``(file_name, content_type, file_bytes)``。"""
    if fmt not in {"docx", "md"}:
        raise ValueError("unsupported export format")

    exported_at = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    stamp = now_utc().strftime("%Y%m%d_%H%M%S")
    short_id = _safe_short_id(str(qa.get("qa_id") or ""))
    question_text = _clean_text(str(qa.get("question") or ""))
    answer_text = _extract_answer_text(qa) or "（无正文输出）"
    resolved_citations = list(
        citations if citations is not None else (qa.get("result") or {}).get("citations") or []
    )
    resolved_steps = list(steps or [])

    if fmt == "md":
        lines = [
            "# 充电桩后台排查报告",
            "",
            f"- 问答编号：`{short_id}`",
            f"- 导出时间：{exported_at}",
            f"- 问答状态：{qa.get('status') or 'completed'}",
            "",
            "## 排查问题",
            "",
            question_text or "（无）",
            "",
        ]
        if resolved_steps:
            lines.extend(["## 执行路径摘要", ""])
            for idx, step in enumerate(resolved_steps, start=1):
                label = _clean_text(str(step.get("label") or step.get("step_id") or f"步骤 {idx}"))
                st = _clean_text(str(step.get("status") or ""))
                summary = _clean_text(str(step.get("summary") or ""))
                suffix = f"：{summary}" if summary else ""
                lines.append(f"{idx}. [{st}] {label}{suffix}")
            lines.append("")
        lines.extend(["## 排查结论与证据正文", "", answer_text, ""])
        if resolved_citations:
            lines.extend(["## 引用来源", ""])
            for idx, cit in enumerate(resolved_citations, start=1):
                title = _clean_text(str(cit.get("title") or f"来源 {idx}"))
                source_type = _clean_text(str(cit.get("source_type") or ""))
                snippet = _clean_text(str(cit.get("snippet") or ""))
                observed = _clean_text(str(cit.get("observed_at") or ""))
                meta = " · ".join(part for part in (source_type, observed) if part)
                line = f"{idx}. **{title}**"
                if meta:
                    line += f"（{meta}）"
                if snippet:
                    line += f"：{snippet}"
                lines.append(line)
            lines.append("")
        lines.extend(["---", "", _READONLY_DISCLAIMER, ""])
        payload = "\n".join(lines).encode("utf-8")
        return f"AI排查报告_{short_id}_{stamp}.md", MD_CONTENT_TYPE, payload

    doc = Document()
    doc.add_heading("充电桩后台排查报告", level=1)
    status_label = qa.get("status") or "completed"
    doc.add_paragraph(f"问答编号：{short_id}  |  导出时间：{exported_at}  |  状态：{status_label}")

    doc.add_heading("排查问题", level=2)
    doc.add_paragraph(question_text or "（无）")

    if resolved_steps:
        doc.add_heading("执行路径摘要", level=2)
        for idx, step in enumerate(resolved_steps, start=1):
            label = _clean_text(str(step.get("label") or step.get("step_id") or f"步骤 {idx}"))
            st = _clean_text(str(step.get("status") or ""))
            summary = _clean_text(str(step.get("summary") or ""))
            suffix = f"：{summary}" if summary else ""
            doc.add_paragraph(f"{idx}. [{st}] {label}{suffix}")

    doc.add_heading("排查结论与证据正文", level=2)
    for paragraph in answer_text.splitlines():
        if paragraph.strip():
            doc.add_paragraph(paragraph)

    if resolved_citations:
        doc.add_heading("引用来源", level=2)
        for idx, cit in enumerate(resolved_citations, start=1):
            title = _clean_text(str(cit.get("title") or f"来源 {idx}"))
            source_type = _clean_text(str(cit.get("source_type") or ""))
            snippet = _clean_text(str(cit.get("snippet") or ""))
            observed = _clean_text(str(cit.get("observed_at") or ""))
            meta = " · ".join(part for part in (source_type, observed) if part)
            line = f"{idx}. {title}"
            if meta:
                line += f"（{meta}）"
            if snippet:
                line += f"：{snippet}"
            doc.add_paragraph(line)

    doc.add_paragraph("")
    doc.add_paragraph(_READONLY_DISCLAIMER)

    buf = io.BytesIO()
    doc.save(buf)
    return f"AI排查报告_{short_id}_{stamp}.docx", DOCX_CONTENT_TYPE, buf.getvalue()
