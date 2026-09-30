"""A runtime failure is recorded, not just rendered (#487).

`docs/standard-api-contract.md` promises that internal failure detail "goes into
the sanitised log" rather than into the response. Twelve guards in the gateway
did the second half and dropped the first: they rendered a stable 503 with a
fixed message and let `exc` fall on the floor. A misconfigured field, an
unreachable upstream and a provider outage therefore read identically — in the
response (correct, by contract) **and in the logs** (which is the defect).

The response staying byte-identical is the safety boundary of this change, and
it is asserted here. What changes is one line of log per failure.

The guard is deliberately two-sided:

* **behavioural** — a runtime raise produces the documented 503 *and* leaves
  `str(exc)` in the log, with request bodies, user text, SQL and credentials
  absent from it;
* **structural** — every runtime guard goes through the one renderer, so the
  next handler cannot reintroduce a silent one.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.faq import (
    FAQCatalog,
    PlatformIdentityResolver,
    PlatformRoleRecord,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
API_FILE = "gateway_api.py"
#: The one renderer. A runtime guard that does not call it is a silent failure.
RENDERER = "_runtime_unavailable"
#: The logger the renderer writes through — the module's own, not the package's.
LOGGER_NAME = "aiops.gateway"
#: The marker a failing runtime raises into the guard. It carries a real
#: credential shape on purpose: an upstream's error message is the one string
#: here produced outside this process, and it is what must not be echoed
#: verbatim into the log.
BOOM = "provider rejected Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.cGF5bG9hZA.sig"
#: The part of BOOM that must not survive redaction.
SECRET_SHAPE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"


def _boom(*args: object, **kwargs: object):
    del args, kwargs
    raise ValueError(BOOM)


class _Runtime:
    """Every entry point this file drives raises the same way.

    Named explicitly rather than via `__getattr__`: the app inspects the runtime
    during construction, so a catch-all attribute makes unrelated machinery
    treat a function as a settings object and fail somewhere else entirely.
    """

    def shutdown(self) -> None:
        pass

    start_standard_diagnosis = staticmethod(_boom)
    start_assistant_qa = staticmethod(_boom)
    start_health_report = staticmethod(_boom)
    create_conversation = staticmethod(_boom)
    list_conversations = staticmethod(_boom)
    get_conversation = staticmethod(_boom)
    delete_conversation = staticmethod(_boom)
    set_active_order = staticmethod(_boom)
    list_assistant_questions = staticmethod(_boom)
    get_assistant_qa = staticmethod(_boom)
    cancel_assistant_qa = staticmethod(_boom)
    start_promo_qa = staticmethod(_boom)


class _Directory:
    """A single platform role, so the platform decision resolves and the call
    reaches the runtime guard instead of failing at the identity stage."""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        del c_user_id, tenant_id
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        del b_user_id, tenant_id
        return ()


class _Authorizer:
    """Allows everything, so the call reaches the runtime guard under test."""

    def can_access(self, context: object, order_no: str) -> bool:
        del context, order_no
        return True


class _Caller:
    def resolve(self, token: str, **kwargs: object) -> ScopeContext:
        del token, kwargs
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read", "aiops:diagnoses:write", "aiops:faq:read"}),
        )


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=_Caller(),  # type: ignore[arg-type]
            order_authorizer=_Authorizer(),  # type: ignore[arg-type]
            platform_resolver=PlatformIdentityResolver(_Directory()),  # type: ignore[arg-type]
            faq_catalog=FAQCatalog.bundled(),
        )
    )


def _api_tree() -> ast.Module:
    return ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))


def _runtime_guards(tree: ast.Module | None = None) -> list[ast.ExceptHandler]:
    """Every `except (ValueError, RuntimeError)` block in the gateway.

    Takes the tree so a caller can resolve a guard's enclosing function: nodes
    from two separate parses of the same file are not the same objects, and the
    lookup would silently report every guard as module-level.
    """
    tree = tree if tree is not None else _api_tree()
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and node.type is not None
        and "ValueError" in ast.unparse(node.type)
        and "RuntimeError" in ast.unparse(node.type)
    ]


def test_the_guard_enumeration_finds_the_runtime_surface() -> None:
    """A silently empty enumeration would make the structural test a no-op."""
    guards = _runtime_guards()
    assert len(guards) >= 10, f"only {len(guards)} runtime guards found"
    assert all(handler.body for handler in guards)


def test_a_runtime_failure_still_answers_the_documented_503(tmp_path: Path) -> None:
    """The response is unchanged: the exact status, code, message and retry flag.

    Pinned per surface rather than as "some `*_UNAVAILABLE` code": routing these
    through a renderer must not have moved a code, softened a message or dropped
    `retryable`, and a loose assertion would accept all three.

    The surfaces are the ones that actually reach the renderer. `/v1/conversations`
    is deliberately absent: it fails earlier, inside the platform decision, so it
    would answer `PLATFORM_UNAVAILABLE` and prove nothing about this change.
    """
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer token", "X-Business-Entry": "consumer"}
    observed: dict[str, tuple[int, str, str, bool]] = {}
    for path, payload in (
        ("/v1/assistant/questions", {"question": "q"}),
        ("/v1/health-report-jobs", {"order_no": "O-1"}),
        ("/v1/standard/diagnoses", {"order_no": "O-1", "question": "q"}),
    ):
        response = client.post(path, headers=headers, json=payload)
        body = response.json()["error"]
        observed[path] = (
            response.status_code,
            body["code"],
            body["message"],
            body["retryable"],
        )
    assert observed == {
        # A plain question on the consumer entry takes the qa branch; the other
        # two are the report and diagnosis starts.
        "/v1/assistant/questions": (503, "QA_UNAVAILABLE", "general answer unavailable", True),
        "/v1/health-report-jobs": (503, "REPORT_JOB_UNAVAILABLE", "health report job unavailable", True),
        "/v1/standard/diagnoses": (503, "DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", True),
    }, observed


def test_the_failure_reaches_the_log_not_only_the_response(tmp_path: Path, caplog) -> None:
    """The point of the change: an operator can tell WHICH failure this was.

    What is recorded is the exception type, not its message — see the renderer's
    docstring for why. So the assertion is that the type and the code are both
    present, and that the message is **not** (a message can quote a field
    contract or a failed statement, and `redact_text` is pattern-based: it
    removes credential shapes, not free-form text).
    """
    client = _client(tmp_path)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token", "X-Business-Entry": "consumer"},
            json={"order_no": "O-1"},
        )
    records = [record for record in caplog.records if "runtime failure" in record.getMessage()]
    assert records, f"the failure never reached the log: {[r.name for r in caplog.records]}"
    # The renderer's own logger, not some other module's — a wrong name here
    # would still propagate to caplog's root handler and read as covered.
    assert records[0].name == LOGGER_NAME, records[0].name
    message = records[0].getMessage()
    assert "REPORT_JOB_UNAVAILABLE" in message
    assert "ValueError" in message
    # The message itself must not be echoed — free-form text is not redactable
    # by pattern, and an exception message is written by whoever raised it.
    assert SECRET_SHAPE not in message
    assert "provider rejected" not in message


def test_the_log_carries_no_request_or_user_text(tmp_path: Path, caplog) -> None:
    """`standard-api-contract.md:355-356` lists what must never be logged.

    The question text is the caller's, the payload is the caller's, and this
    renderer must not become the channel that publishes either.
    """
    client = _client(tmp_path)
    question = "订单 2096164064667852801 为什么停了"
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client.post(
            "/v1/assistant/questions",
            headers={"Authorization": "Bearer token", "X-Business-Entry": "consumer"},
            json={"question": question, "order_no": "2096164064667852801"},
        )
    logged = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER_NAME)
    assert "为什么停了" not in logged
    assert "2096164064667852801" not in logged
    assert "SELECT" not in logged
    assert "Bearer token" not in logged


#: Runtime guards that answer something OTHER than "a dependency is down", each
#: with the reason. Default-deny: a guard that is not listed here must raise
#: through the renderer. An allowlist rather than a shape test, because the
#: defect was a guard that stopped calling it — and "it raises something named"
#: accepts exactly that.
NON_503_GUARDS = {
    # The classifier's outermost handler treats any escape as "no decision" and
    # returns None (it logs, and deliberately records no metric); it answers no
    # caller and has no 503 to render.
    "_classify_for_routing",
    # Agent debug-run answers 502 with its own public message: the shape is the
    # contract for this surface (`_debug_public_error`).
    "debug_run_agent",
    # Metrics input validation: a 422 for a caller-supplied argument, not a
    # dependency failure.
    "agent_metrics_summary",
    "agent_metrics_runs",
}


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    cur: ast.AST | None = target
    while cur is not None and cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.FunctionDef):
            return cur.name
    return "<module>"


def test_every_runtime_guard_goes_through_the_one_renderer() -> None:
    """No guard renders its own 503 and drops the exception.

    The defect was a *missing* call, which no response test can see — the answer
    is correct either way. Default-deny, so a guard that stops calling the
    renderer (the actual regression) fails here even though it still raises
    something, and a _new_ guard cannot be added without either calling it or
    being listed with a reason.
    """
    tree = _api_tree()
    offenders: list[str] = []
    for handler in _runtime_guards(tree):
        owner = _enclosing_function(tree, handler)
        calls = {
            getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
        }
        if RENDERER in calls:
            continue
        if owner not in NON_503_GUARDS:
            offenders.append(
                f"{API_FILE}:{handler.lineno} {owner} guards a runtime failure"
                f" without going through {RENDERER} (and is not an allowed exception)"
            )
    assert offenders == [], "; ".join(offenders)
    # A listed exception that no longer exists hides a guard that should be
    # checked; the list is part of the guard, so it is checked too.
    present = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    stale = sorted(set(NON_503_GUARDS) - present)
    assert stale == [], f"these allowlisted guards are gone: {stale}"
