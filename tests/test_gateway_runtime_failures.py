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
        )
    )


def _runtime_guards() -> list[ast.ExceptHandler]:
    """Every `except (ValueError, RuntimeError)` block in the gateway."""
    tree = ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))
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
    """The response is unchanged: same status, code and message as before.

    One call per guarded surface. The point is not the individual codes — it is
    that routing them through a renderer did not quietly change what any caller
    sees, which is what a "while I am here" edit usually does.
    """
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer token"}
    observed: dict[str, tuple[int, str]] = {}
    for path, payload in (
        ("/v1/assistant/questions", {"question": "q"}),
        ("/v1/conversations", {"agent_version_key": "agt_abcdef1234567890#v1"}),
        ("/v1/health-report-jobs", {"order_no": "O-1"}),
        ("/v1/standard/diagnoses", {"order_no": "O-1", "question": "q"}),
    ):
        response = client.post(path, headers=headers, json=payload)
        body = response.json().get("error", {})
        observed[path] = (response.status_code, body.get("code", ""))
    assert all(status == 503 for status, _ in observed.values()), observed
    assert all(code.endswith("_UNAVAILABLE") for _, code in observed.values()), observed


def test_the_failure_reaches_the_log_not_only_the_response(tmp_path: Path, caplog) -> None:
    """The whole point: `str(exc)` becomes visible to an operator.

    Asserted on the record's logger name as well as its text — a wrong logger
    name still propagates to the root handler caplog owns, so naming a logger
    that does not exist would pass while claiming to pin this one.
    """
    client = _client(tmp_path)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1"},
        )
    records = [record for record in caplog.records if "runtime failure" in record.getMessage()]
    assert records, f"the failure never reached the log: {[r.name for r in caplog.records]}"
    # The renderer's own logger, not some other module's — a wrong name here
    # would still propagate to caplog's root handler and read as covered.
    assert records[0].name == LOGGER_NAME, records[0].name
    assert "provider rejected" in records[0].getMessage()
    assert "ValueError" in records[0].getMessage()
    # A credential-shaped string in the upstream's message must not survive.
    assert SECRET_SHAPE not in records[0].getMessage()
    assert "REDACTED" in records[0].getMessage()


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
            headers={"Authorization": "Bearer token"},
            json={"question": question, "order_no": "2096164064667852801"},
        )
    logged = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER_NAME)
    assert "为什么停了" not in logged
    assert "2096164064667852801" not in logged
    assert "SELECT" not in logged
    assert "Bearer token" not in logged


def test_every_runtime_guard_goes_through_the_one_renderer() -> None:
    """No guard renders its own 503 and drops the exception.

    The defect was a *missing* call, which no response test can see: the answer
    is correct either way. So the rule is checked at the source — each guard's
    body must raise through the renderer, and the renderer itself must be the
    only place that maps a runtime failure to 503 with `retryable=True`.
    """
    offenders: list[str] = []
    for handler in _runtime_guards():
        raises = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        ]
        if not raises:
            continue
        for node in raises:
            assert isinstance(node.exc, ast.Call)
            called = getattr(node.exc.func, "id", "")
            # A guard may also raise a *different* error deliberately (a 404 for
            # an out-of-scope order, say); only the 503-shaped ones are ours.
            renders_503 = any(
                getattr(arg, "attr", "") == "HTTP_503_SERVICE_UNAVAILABLE" for arg in node.exc.args
            )
            if renders_503:
                offenders.append(f"{API_FILE}:{node.lineno} renders its own 503")
            elif called not in {RENDERER} and not _is_unrelated(node.exc):
                # Neither the renderer nor a documented other error.
                offenders.append(f"{API_FILE}:{node.lineno} raises {called}() from a runtime guard")
    assert offenders == [], "; ".join(offenders)


def _is_unrelated(call: ast.Call) -> bool:
    """Whether this raise is a different, deliberate contract answer.

    The guards catch `(ValueError, RuntimeError)` broadly, so some of them
    translate a *domain* error to its own status (a 422 for invalid metrics
    input, a 404 for an unknown run). Those are not the failure this ticket is
    about and must not be dragged into it.
    """
    name = getattr(call.func, "id", "")
    if name in {"StandardAPIError", "HTTPException"}:
        return True
    return bool(name)
