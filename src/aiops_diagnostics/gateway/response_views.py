"""Gateway HTTP 响应序列化、统一 503 运行时故障日志、澄清与指标辅助。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from typing import Any

from fastapi import Request, status

from aiops_diagnostics.common.async_job_lifecycle import DIAGNOSIS, QUESTION
from aiops_diagnostics.common.i18n import (
    DEFAULT_LANGUAGE,
    QA_FALLBACK_MESSAGES,
    clarification_message,
    diagnosis_error_message,
    effective_language,
    free_text_unavailable_message,
    localized_pack,
)
from aiops_diagnostics.common.model_request import ModelRequestError
from aiops_diagnostics.conversation.clarification import (
    CLARIFY_LANGUAGE_UNROUTABLE,
    MESSAGE_KEY_BY_CODE,
    missing_fields_for,
)
from aiops_diagnostics.gateway import metrics_store as metrics_dims
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.gateway_store import (
    ACTIVE_DIAGNOSIS_STATUSES,
    ACTIVE_HEALTH_JOB_STATUSES,
    TERMINAL_DIAGNOSIS_STATUSES,
)
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.operator import operator_cpms
from aiops_diagnostics.publication import agent_content

_LOGGER = logging.getLogger("aiops.gateway")

#: The only failure code that means retrieval actually failed. Everything else
#: on this surface — a provider error, a contract violation, an unreachable
#: model — records `QA_FAILED` and has nothing to do with the knowledge base.
_KB_FAILURE_CODE = "KB_UNAVAILABLE"

#: `OPERATOR_HISTORY_LIMIT` 的用户文案。与 `QA_FALLBACK_MESSAGES` 同一张
#: 「按语言查词表、缺行读声明 fallback」的取法（`localized_pack`）。
_HISTORY_LIMIT_MESSAGES = {
    "zh": "这段对话的证据引用已达到上限，请新建对话继续。",
    "zh-Hant": "這段對話的證據引用已達到上限，請新建對話繼續。",
    "en": "This conversation reached its evidence limit. Start a new conversation to continue.",
}

#: 只有本切片的端点拒绝中间层缓存：它们是**按人**的授权结果，不是公共资源。
NO_STORE_PATH_PREFIXES = (
    "/v1/conversations",
    "/v1/assistant/questions",
    "/v1/dify/ops/",
    "/v1/orders/",
    "/v1/internal/charging-status/",
    "/v1/internal/operator-runs/",
    "/v1/operator-tools/",
)

#: The message every "a runtime dependency is down" answer carries. The body is
#: unchanged from the 12 inline copies this replaces — the point of the helper is
#: that the failure is *recorded*, not that the text improves.
_RUNTIME_FAILURE_MESSAGE = "runtime dependency unavailable"


def _no_store_headers(request: Request) -> dict[str, str]:
    """错误响应也带 ``Cache-Control: no-store``，但**只**对受控路径。"""
    if request.url.path.startswith(NO_STORE_PATH_PREFIXES):
        return {"Cache-Control": "no-store"}
    return {}


def _hash_identifier(value: str) -> str:
    """Short, non-reversible marker for a log line — never the identifier itself."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _record_route_metric(
    context: Any,
    caller: ScopeContext | None,
    *,
    route_type: str,
    outcome: str,
    agent_id: str | None = None,
    agent_version_key: str | None = None,
    conversation_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """Best-effort redacted route metric (T7/#174); never fails the request."""
    if caller is None:
        return
    recorder = getattr(context.runtime, "record_route_metric", None)
    if recorder is None:
        return
    with contextlib.suppress(Exception):
        recorder(
            caller,
            route_type,
            outcome,
            agent_id=agent_id,
            agent_version_key=agent_version_key,
            conversation_id=conversation_id,
            error_code=error_code,
        )


def _health_job_response(job: dict[str, Any]) -> dict[str, Any]:
    status_value = str(job["status"])
    return {
        "job_id": job["job_id"],
        "order_no": job["order_no"],
        "rule_version": job["rule_version"],
        "status": status_value,
        "retry_after_ms": 1000 if status_value in ACTIVE_HEALTH_JOB_STATUSES else None,
        # The report's prose language: the language the JOB was created in.
        # It describes the stored `report`, not this request — a poller cannot
        # relabel a report it did not create (same rule as the diagnosis
        # surface; see `_standard_diagnosis_response`).
        "language": job.get("language") or "zh",
        "report": job.get("report"),
        "error": (
            {
                "code": job.get("error_code") or "REPORT_FAILED",
                "message": job.get("error_message") or "health report failed",
                # A restart is retryable for the caller: the report never ran,
                # and asking again is exactly what should happen. Without it
                # here, the frontend shows an error with no action — which is
                # the one thing the restart verdict is supposed to avoid.
                "retryable": job.get("error_code")
                in {"SOURCE_UNAVAILABLE", "REPORT_TIMEOUT", "REPORT_INTERRUPTED_BY_RESTART"},
            }
            if status_value == "failed"
            else None
        ),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_at": job.get("completed_at"),
    }


def _qa_user_message(language: str, error_code: str | None) -> str:
    """The localized, actionable sentence a customer sees when a QA job fails.

    The cause decides the sentence. Claiming the knowledge base is unavailable
    for every failure told a customer whose model provider had simply rejected
    the request that the *library* was down — a false cause and useless
    guidance, since retrying is the wrong advice only for the reader who
    believes the wrong thing is broken. Only a verified retrieval failure gets
    the retrieval copy; the rest get the honest, cause-neutral one.
    """
    if error_code == "OPERATOR_HISTORY_LIMIT":
        return localized_pack(_HISTORY_LIMIT_MESSAGES, language)
    if error_code in operator_cpms.NODE_FAILURE_CODES.values():
        return diagnosis_error_message(language, error_code)
    pack = localized_pack(QA_FALLBACK_MESSAGES, language)
    return pack["unavailable"] if error_code == _KB_FAILURE_CODE else pack["generation_failed"]


def _assistant_question_response(qa: dict[str, Any], language: str) -> dict[str, Any]:
    """The one public shape of an assistant-question job.

    Poll and cancel return the same body on purpose: the job's state is the
    single thing both surfaces report, so a client reads a stopped job exactly
    as it reads a finished one.

    The failure message is customer-facing copy, not the job record's internal
    reason. The record keeps that reason deliberately — a contract violation is
    a defect an engineer must be able to see, and replacing it there once turned
    a real bug into "service temporarily unavailable" (2026-09-17). But the
    record is read by an engineer and this response is read by a customer; the
    two need different sentences, and only the boundary can tell them apart. A
    Chinese-speaking user was shown "customer QA turn returned invalid JSON",
    which names neither the problem nor an action.
    """
    status_value = str(qa["status"])
    effective_lang = qa.get("language") or effective_language(language)
    return {
        "type": "qa",
        # The EFFECTIVE language: a non-promptable request is answered in the
        # default, so reporting the raw request tag would describe text the
        # answer does not contain (#541 review).
        "language": effective_lang,
        "qa_id": qa["qa_id"],
        "question": qa["question"],
        "status": status_value,
        "attempt_no": int(qa.get("attempt_no") or 1),
        "parent_qa_id": qa.get("parent_qa_id"),
        "retry_after_ms": 1000 if status_value in ACTIVE_DIAGNOSIS_STATUSES else None,
        "stream_url": f"/v1/assistant/questions/{qa['qa_id']}/events",
        "agent": agent_content.operator_agent_brief(
            effective_lang, str(qa.get("agent_id") or agent_content.OPERATOR_MAIN_AGENT_ID)
        ),
        "result": qa.get("result"),
        "error": (
            {
                "code": qa.get("error_code") or "QA_FAILED",
                "message": _qa_user_message(language, qa.get("error_code")),
                "retryable": qa.get("error_code") != "OPERATOR_MODEL_QUOTA_EXHAUSTED",
            }
            if status_value in {QUESTION.failed, QUESTION.expired}
            else None
        ),
    }


def _conversation_response(conversation: dict[str, Any]) -> dict[str, Any]:
    """Public conversation shape (no internal fingerprints)."""
    return {
        "conversation_id": conversation["conversation_id"],
        "business_entry": conversation["business_entry"],
        "agent_id": conversation.get("agent_id") or "operator_main",
        "agent_version_key": conversation["agent_version_key"],
        "title": str(conversation.get("title") or ""),
        "active_order_no": conversation["active_order_no"],
        "is_generating": conversation["is_generating"],
        "turn_count": int(conversation.get("turn_count") or 0),
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "expires_at": conversation["expires_at"],
    }


def _clarification(
    context: Any,
    caller: ScopeContext | None,
    decision: Any,
    *,
    code: str,
    question: str,
    language: str,
) -> dict[str, Any]:
    """The one place a clarification reply is built and recorded (#636).

    Before this, six branches each hand-built the same envelope and recorded an
    indistinguishable `route_type='clarification'` row. Three things varied by
    accident rather than by decision: whether the log line carried a stable
    identifier, whether the metric row did, and whether the code and the message
    could disagree. Now the CODE picks the fields — `missing_fields` and the
    message key both come from the registry in `clarification.py` — so a branch
    cannot ship a code that contradicts its own copy, and adding a branch without
    registering its code fails a test rather than going quiet.

    The metric rides the existing `error_code` column. It is not an error, and
    that is a deliberate reuse: the column is the row's machine-readable
    discriminant, renaming it would touch every reader, and a clarification that
    is not distinguishable from the other five is the defect this closes.
    """
    missing = missing_fields_for(code)
    if code == CLARIFY_LANGUAGE_UNROUTABLE:
        message = free_text_unavailable_message(language)
    else:
        message = clarification_message(language, MESSAGE_KEY_BY_CODE[code])
    _record_route_metric(
        context, caller, route_type=metrics_dims.ROUTE_CLARIFICATION, outcome="completed", error_code=code
    )
    # The structural line the ticket asked for, minus the two things that must
    # never be in it: no identity, no order number — only the hashes the FAQ
    # module already logs for the same reason. It has somewhere to go now:
    # `gateway_logging` attaches the root handler uvicorn never did.
    _LOGGER.info(
        "clarification_answered code=%s language=%s missing=%s subject_hash=%s tenant_hash=%s",
        code,
        language,
        ",".join(missing) or "-",
        _hash_identifier(getattr(getattr(caller, "subject", None), "b_user_id", "") or ""),
        _hash_identifier(getattr(caller, "effective_tenant_id", "") or ""),
    )
    return {
        **decision.public(),
        "type": "clarification",
        "language": language,
        "question": question,
        "missing_fields": missing,
        "message": message,
        "code": code,
    }


def _runtime_unavailable(code: str, message: str, exc: Exception) -> StandardAPIError:
    """Render a runtime failure as the 503 the contract promises, and record it.

    The contract (`docs/standard-api-contract.md:355-356`) says internal failure
    detail goes to the sanitised log and that the response carries no exception
    type, SQL, absolute path, password or raw upstream response. The 12 copies
    this replaces did the second half and dropped `exc` entirely: a misconfigured
    field, an unreachable upstream and a provider outage read identically in the
    response (correct, by contract) **and in the log** (the defect).

    What is logged is **the exception type and the failure's own vocabulary** —
    never `str(exc)`. That is the narrower choice, and it is deliberate:

    * `redact_text` is pattern-based. It removes credential *shapes*; it does not
      remove free-form text, and a `SourceError` may quote a field contract or a
      backend response while an upstream driver's message may quote the statement
      that failed. Passing those through would put request-adjacent content into
      gateway logs, which is the very thing the contract forbids.
    * The type plus the error code is what tells an operator which failure they
      are looking at: `SourceError`/`MYSQL_UNAVAILABLE` is a backend problem,
      `AgentRuntimeError`/`PROVIDER_UNAVAILABLE` is a provider problem. That is
      the whole diagnostic question this ticket exists to answer.

    ModelRequestError carries only locally classified reason/status. Its
    non-retryable dependency failures need operator action, not a retry loop.
    Raw provider messages remain excluded from both the response and the log.
    """
    if isinstance(exc, ModelRequestError):
        _LOGGER.warning(
            "runtime failure -> %s (%s) reason=%s http_status=%s",
            code,
            type(exc).__name__,
            exc.reason,
            exc.http_status,
        )
    else:
        _LOGGER.warning("runtime failure -> %s (%s)", code, type(exc).__name__)
    return StandardAPIError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        code,
        message,
        retryable=exc.retryable if isinstance(exc, ModelRequestError) else True,
    )


def _shortcut_list_language(shortcuts: Any, requested: str) -> str:
    """The language a shortcut listing is uniformly in, else the authority one.

    Rows are published independently, so a listing can mix a translated row with
    an untranslated one. A single top-level field cannot describe that, and
    claiming the requested language for a mixed list is the same false statement
    as claiming it for one fallback row — so it reports the shared language only
    when there is one, and the per-row `language` carries the truth otherwise.
    """
    if not shortcuts:
        # No rows means no text, so there is nothing to contradict the request:
        # reporting the authority language here would describe a payload that
        # does not exist.
        return requested
    served = {item.served_language(requested) for item in shortcuts}
    return served.pop() if len(served) == 1 else DEFAULT_LANGUAGE


def _analysis_job_response(job: dict[str, Any], language: str) -> dict[str, Any]:
    """One analysis row as the public body (第三份契约，see ``ops_analysis``).

    Deliberately NOT ``_standard_diagnosis_response``: that builder is the
    single-order face, and its ``order_no`` / ``question`` / ``indicator_code``
    fields describe an incident this job does not have. Reusing it would put an
    empty ``order_no`` on the wire for an analysis — the shape equivalent of
    saying "this card is about an order".
    """
    status_value = str(job["status"])
    error_code = job.get("error_code") or "ANALYSIS_FAILED"
    is_terminal = status_value in TERMINAL_DIAGNOSIS_STATUSES
    return {
        "analysis_id": job["diagnosis_id"],
        "kind": job.get("kind") or "analysis",
        # Which card this job is. A poller that only remembers the id still gets
        # the right shape back, instead of having to remember the route it used.
        "card": job.get("card") or "failure_rate",
        "site_ids": json.loads(job["site_ids_json"]) if job.get("site_ids_json") else None,
        "window_start": job.get("window_start"),
        "window_end": job.get("window_end"),
        "rule_version": job.get("rule_version"),
        "language": job.get("language", "zh"),
        "status": status_value,
        "retry_after_ms": None if is_terminal else 1000,
        "result": job.get("result") if status_value in DIAGNOSIS.completed else None,
        "error": (
            {
                "code": error_code,
                # Same split as the diagnosis face: the message is rendered per
                # request from the code; the stored note is the engineer's.
                "message": diagnosis_error_message(language, error_code),
                "retryable": status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired},
            }
            if status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired}
            else None
        ),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
    }
