"""One attributable line per consumer order-status request.

**The gap this closes.** `POST /v1/assistant/questions` had two observation
holes, both measured on 41 by an independent probe (2026-10-23):

* a successful consumer status query wrote an `agent_run_metrics` row but **no
  `duration_ms`**, and that row carries neither an HTTP status nor a `qa_id`;
* every refusal — 403 `INSUFFICIENT_SCOPE`, 404 `ORDER_NOT_FOUND`, 404
  `TENANT_ENDPOINT_UNRESOLVED`, 502 `CHARGING_STATUS_UNAVAILABLE` — wrote
  **nothing at all**. `_consumer_order_chat` raises before the success metric,
  and `_order_authorization`'s own `order_auth` row is a *sub-call* metric, so
  counting it in the request denominator would be wrong.

The consequence was that the change ticket's stop conditions could not be
evaluated: "was that 404 ours or a different one" was unanswerable, and the
`139/148 ms` quoted there was in fact only the span after the QA row existed —
SQL, CPMS and identity resolution all happened before it.

**Why a log line and not a second metric table.** The gateway already has one
redacted telemetry surface (`agent_run_metrics`) whose retention, tenant
scoping and shape are load-bearing. Adding a parallel table for the same
request would create two denominators that drift. The journal already carries
structured lines from `aiops.` loggers through `gateway_logging`, is already
collected, and is where an operator looks first. So this emits exactly one
`INFO` line per candidate request and writes no database row.

**What "candidate" means — and what it deliberately does not.** A request is
recorded only after a *verified* caller exists and the routing predicate for
the deterministic branch holds (consumer platform, no shortcut, zh/zh-Hant/en,
`is_order_status_question`). Two consequences matter:

* a request rejected while establishing identity (401/403 from the resolver)
  is **never** recorded — nothing here may claim "this hit the personal query
  branch" when no identity was established, and no tenant may come from a
  client-reported header;
* the classification is a pure function of values the handler has already
  computed. It reads `caller`, `decision`, `payload` and `language`; it does
  not reorder, short-circuit or alter any route.

**Timing.** `started` is taken when the ASGI scope enters this middleware and
stopped after the response has been handed to the server, so it covers identity
resolution, the pre-route order-authorization guard, the SQL and CPMS calls, and
the QA write. It does **not** cover TLS, nginx, or time spent in the server's
own read/write buffers before this scope existed — see
`docs/agents/consumer-order-chat/observability.md` for the measurement contract.

**A logging failure must never change a response.** Every emission is wrapped;
a broken handler is swallowed. The business answer is not this module's to fail.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger("aiops.assistant_question")

#: Where a request was when it ended. Ordered by how far it got, so a reader can
#: tell "rejected before routing" from "rejected inside the branch".
STAGE_UNCLASSIFIED = "unclassified"
STAGE_CLASSIFIED = "classified"
STAGE_PRE_ROUTE_ORDER_AUTH = "pre_route_order_authorization"
STAGE_ORDER_CHAT_AUTH = "order_chat_authorization"
STAGE_ORDER_CHAT_LOOKUP = "order_chat_lookup"
STAGE_ORDER_CHAT_CPMS = "order_chat_cpms"
STAGE_ORDER_CHAT_EVIDENCE = "order_chat_evidence"

OUTCOME_COMPLETED = "completed"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"

#: Stable codes for the two cases that have no `StandardAPIError` of their own.
UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"
INVALID_REQUEST = "INVALID_REQUEST"

#: Same shape the metrics store enforces, so a malformed code cannot smuggle
#: free text (an exception message, a URL) into the line.
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_QA_ID = re.compile(r"^qa_[0-9a-f]{32}$")

#: Which HTTP statuses count as a refusal rather than a failure. 4xx is "we said
#: no"; 5xx and an escaped exception are "it went wrong".
_REFUSAL_STATUSES = frozenset({400, 403, 404, 409, 422})


def new_request_id() -> str:
    """A server-generated correlation id. Never taken from a client header.

    The point is to join one request's log line to the request itself even when
    two arrive in the same second; a client-supplied value would let a caller
    choose the correlation and collide two requests on purpose.
    """
    return "cor_" + secrets.token_hex(6)


def tenant_hash(tenant_id: str) -> str:
    """Redacted tenant marker — the same rule `gateway_api._hash_identifier` uses.

    Kept here rather than imported so this module stays a leaf: the hot path
    calls it, and importing the API module from a request-scoped helper would
    make the dependency circular.
    """
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]


@dataclass
class Observation:
    """What this middleware knows about one request. Never leaves the process."""

    request_id: str
    started: float = field(default_factory=time.monotonic)
    candidate: bool = False
    stage: str = STAGE_UNCLASSIFIED
    qa_id: str | None = None
    tenant: str = ""
    error_code: str = ""

    # -- classification (called from the route, after identity is verified) --

    def classify(self, *, candidate: bool) -> None:
        self.candidate = candidate
        self.stage = STAGE_CLASSIFIED if candidate else STAGE_UNCLASSIFIED

    def advance(self, stage: str) -> None:
        """Record how far the request got. Only meaningful for a candidate."""
        if self.candidate:
            self.stage = stage

    def attach_qa(self, qa_id: str) -> None:
        """The successful request's own id, so a log line can be joined to the job."""
        if self.candidate and _QA_ID.match(str(qa_id)):
            self.qa_id = str(qa_id)

    def attach_tenant(self, tenant_id: str) -> None:
        """Only ever called with a tenant from a *verified* caller."""
        if self.candidate and tenant_id:
            self.tenant = tenant_hash(tenant_id)

    def refuse(self, error_code: str) -> None:
        if self.candidate:
            self.error_code = error_code


def classify_request(
    request: Any,
    *,
    consumer_order_chat_enabled: bool,
    platform: str,
    shortcut_code: str | None,
    language: str,
    question: str,
    is_status_question: Any,
) -> Observation | None:
    """Decide whether this request is a candidate, and mark how far it got.

    Called from the handler **after** identity is verified and after the
    platform decision exists — never before, because a request whose caller
    could not be established has no tenant this module may trust, and must not
    be described as having reached the personal-query branch.
    """
    record = observation_of(request)
    if record is None:
        return None
    record.classify(
        candidate=bool(
            consumer_order_chat_enabled
            and str(platform) == "consumer"
            and language in {"zh", "zh-Hant", "en"}
            and not shortcut_code
            and is_status_question(question)
        )
    )
    return record


def mark_stage(request: Any, stage: str) -> None:
    record = observation_of(request)
    if record is not None:
        record.advance(stage)


def mark_refused(request: Any, error_code: str) -> None:
    record = observation_of(request)
    if record is not None:
        record.refuse(error_code)


def mark_qa(request: Any, qa_id: str) -> None:
    record = observation_of(request)
    if record is not None:
        record.attach_qa(qa_id)


def mark_tenant(request: Any, tenant_id: str) -> None:
    record = observation_of(request)
    if record is not None:
        record.attach_tenant(tenant_id)


def scope_state(scope: dict[str, Any]) -> dict[str, Any]:
    """`scope["state"]` — created here if the server did not provide one."""
    return scope.setdefault("state", {})


def observation_of(request: Any) -> Observation | None:
    """The record for this request, or None when observation is not installed."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    current = (scope.get("state") or {}).get("assistant_question_observation")
    return current if isinstance(current, Observation) else None


def _outcome(http_status: int | None, error_code: str) -> str:
    if error_code == UNHANDLED_EXCEPTION or (http_status or 0) >= 500:
        return OUTCOME_FAILED
    if http_status is not None and http_status in _REFUSAL_STATUSES:
        return OUTCOME_REFUSED
    if http_status is not None and 200 <= http_status < 300:
        return OUTCOME_COMPLETED
    return OUTCOME_FAILED


def format_line(record: Observation, *, http_status: int | None) -> str:
    """One line, fixed field order, no free text. Safe to grep and to sum."""
    # Empty is the normal success case, so it renders as the same "-" every
    # other absent field uses; a non-empty value that is not a code is a defect
    # and becomes UNKNOWN rather than leaking whatever it was.
    code = (
        "-"
        if not record.error_code
        else (record.error_code if _SAFE_CODE.match(record.error_code) else "UNKNOWN")
    )
    parts = [
        f"request_id={record.request_id}",
        "route=consumer_order_chat",
        f"stage={record.stage}",
        f"outcome={_outcome(http_status, code)}",
        f"http_status={http_status if http_status is not None else 0}",
        f"error_code={code}",
        f"tenant_hash={record.tenant or '-'}",
        f"qa_id={record.qa_id or '-'}",
        f"duration_ms={int((time.monotonic() - record.started) * 1000)}",
    ]
    return "assistant_question_result " + " ".join(parts)


def emit(record: Observation, *, http_status: int | None) -> None:
    """Write the line. Never raises, never touches the response."""
    if not record.candidate:
        return
    # Observation is best-effort by construction: a broken log handler must not
    # turn an answer the user already earned into a 500.
    with contextlib.suppress(Exception):
        _LOGGER.info(format_line(record, http_status=http_status))


class AssistantQuestionObservation:
    """ASGI middleware: one correlation id, one line, per candidate request.

    Written as a raw ASGI middleware rather than `BaseHTTPMiddleware` so the
    status is read from the message the app actually sends; a route that raises
    before any response is produced leaves `http_status` unset and is recorded
    as a failed request rather than silently skipped.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        record = Observation(request_id=new_request_id())
        scope_state(scope)["assistant_question_observation"] = record
        status: int | None = None

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The exception escapes to Starlette's own 500 handler; record it
            # here because that handler never reaches this module.
            record.refuse(UNHANDLED_EXCEPTION)
            emit(record, http_status=500)
            raise
        else:
            emit(record, http_status=status)


__all__ = [
    "AssistantQuestionObservation",
    "INVALID_REQUEST",
    "Observation",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_REFUSED",
    "STAGE_CLASSIFIED",
    "STAGE_ORDER_CHAT_AUTH",
    "STAGE_ORDER_CHAT_CPMS",
    "STAGE_ORDER_CHAT_EVIDENCE",
    "STAGE_ORDER_CHAT_LOOKUP",
    "STAGE_PRE_ROUTE_ORDER_AUTH",
    "STAGE_UNCLASSIFIED",
    "UNHANDLED_EXCEPTION",
    "classify_request",
    "emit",
    "format_line",
    "mark_qa",
    "mark_refused",
    "mark_stage",
    "mark_tenant",
    "new_request_id",
    "observation_of",
    "tenant_hash",
]
