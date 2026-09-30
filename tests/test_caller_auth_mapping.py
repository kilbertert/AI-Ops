"""One mapping from `CallerAuthError` to the outbound error, for every caller.

The repository had three copies of that mapping and one of them had no
`CALLER_AUTH_CONFIG_MISSING` branch. The consequence was not cosmetic: a
gateway whose own introspection configuration was missing answered twelve
endpoints with **401 `INVALID_ACCESS_TOKEN`**, which is the frontend contract's
signal for "your login is gone, sign in again" — while `docs/gateway.md` says a
missing configuration is a stable **503**, fail closed. The user-visible result
is a login loop for a server-side misconfiguration.

Two properties are pinned here, and they need different kinds of test:

* **Every endpoint that authenticates through `caller_resolver` maps the same
  way.** The route list is enumerated from the source, not typed out, because
  the defect was "one branch that nobody looked at" — a hand-written list is
  exactly the thing that would have missed it.
* **There is no second mapping.** A source-level assertion, because a new
  dependency could otherwise reintroduce one and every behaviour test above
  would still pass.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_CONFIG_MISSING,
    CALLER_AUTH_FORBIDDEN,
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
API_FILE = "gateway_api.py"
#: The shared entry point. One definition, and every dependency delegates to it.
SHARED = "_authenticate_caller"
#: The dependencies that authenticate a caller (as opposed to a device token).
SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
API_FILE = "gateway_api.py"
CALLER_DEPENDENCIES = (
    "authenticated_caller",
    "authenticated_diagnosis_caller",
    "authenticated_faq_caller",
    "authenticated_agent_caller",
    "authenticated_shortcut_caller",
    "authenticated_shortcut_viewer",
)
#: The error codes this mapping may produce, and whether a retry can help.
#: `ACCESS_TOKEN_VALIDATION_UNAVAILABLE` is the fail-closed answer for "we could
#: not check your token", which is not the caller's fault and not a login state.
RETRYABLE_CODES = {"ACCESS_TOKEN_VALIDATION_UNAVAILABLE"}


class _Runtime:
    def shutdown(self) -> None:
        pass


class _Resolver:
    def __init__(self, outcome: ScopeContext | Exception) -> None:
        self.outcome = outcome

    def resolve(self, token: str, **kwargs: object) -> ScopeContext:
        del token, kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _scope() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="TENANT-1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="TENANT-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
    )


def _client(tmp_path: Path, resolver: _Resolver) -> TestClient:
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
            caller_resolver=resolver,  # type: ignore[arg-type]
        )
    )


def _dependant_names(dependant: object) -> set[str]:
    """Every callable name in a route's dependency tree, transitively.

    Walked over the built app rather than parsed from the source: the question
    is "which endpoints does this mapping actually govern", and only FastAPI
    knows that after `Depends` chains are resolved — `assistant_questions`
    depends on `assistant_identity`, which depends on the authenticated caller.
    A source reader that stopped at the first level would have found a handful
    of routes and called the surface covered.
    """
    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for child in getattr(dependant, "dependencies", ()) or ():
        names |= _dependant_names(child)
    return names


def _routes_using_caller_resolver(client: TestClient) -> list[tuple[str, str]]:
    """`(method, path)` for every route that authenticates a caller.

    Read from the built application, so a route added later is covered without
    anyone remembering to add it here.
    """
    routes: list[tuple[str, str]] = []
    for route in client.app.routes:  # type: ignore[attr-defined]
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        if _dependant_names(dependant) & set(CALLER_DEPENDENCIES):
            routes.append((sorted(route.methods)[0], route.path))
    return sorted(set(routes))


def test_the_route_enumeration_finds_the_authenticated_surface() -> None:
    """The enumeration is the test's own foundation; a silent empty list would
    turn every assertion below into a green no-op."""
    routes = _routes_using_caller_resolver(_client(Path(tempfile.mkdtemp()), _Resolver(_scope())))
    assert len(routes) >= 20, f"only {len(routes)} routes found: {routes}"
    assert any("/v1/assistant/questions" in path for _, path in routes)
    assert any("/v1/faq/" in path for _, path in routes)


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_code"),
    [
        # The regression this file exists for: a gateway configuration gap.
        (
            CallerAuthError("no introspection", code=CALLER_AUTH_CONFIG_MISSING),
            503,
            "ACCESS_TOKEN_VALIDATION_UNAVAILABLE",
        ),
        # A caller-side failure keeps its own meaning.
        (CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN), 403, "INSUFFICIENT_SCOPE"),
        (CallerAuthError("token rejected", code=CALLER_AUTH_INVALID), 401, "INVALID_ACCESS_TOKEN"),
        (
            CallerAuthError("directory down", code=CALLER_AUTH_UNAVAILABLE, retryable=True),
            503,
            "ACCESS_TOKEN_VALIDATION_UNAVAILABLE",
        ),
    ],
)
def test_every_authenticated_route_maps_a_resolver_failure_the_same_way(
    tmp_path: Path, outcome: Exception, expected_status: int, expected_code: str
) -> None:
    """Same failure, same answer — on every route, not on a hand-picked few."""
    resolver = _Resolver(outcome)
    client = _client(tmp_path, resolver)
    headers = {"Authorization": "Bearer token"}
    mismatches: list[str] = []
    for method, path in _routes_using_caller_resolver(client):
        request_path = path.replace("{qa_id}", "qa_x").replace("{diagnosis_id}", "dx_x")
        request_path = request_path.replace("{question_id}", "q1").replace("{job_id}", "job1")
        request_path = request_path.replace("{order_no}", "O-1").replace("{code}", "c1")
        request_path = request_path.replace("{id}", "m1").replace("{agent_id}", "agt1")
        response = client.request(
            method, request_path, headers=headers, json={} if method == "POST" else None
        )
        if (response.status_code, response.json().get("error", {}).get("code")) != (
            expected_status,
            expected_code,
        ):
            mismatches.append(f"{method} {request_path} -> {response.status_code} {response.text[:120]}")
    assert mismatches == [], "; ".join(mismatches)


def test_a_config_missing_error_is_retryable_wherever_it_is_answered(tmp_path: Path) -> None:
    """503 means "try again", so `retryable` must say so.

    A non-retryable 503 tells the client to give up on something that a
    configuration fix restores.
    """
    client = _client(tmp_path, _Resolver(CallerAuthError("gap", code=CALLER_AUTH_CONFIG_MISSING)))
    sample = client.post(
        "/v1/assistant/questions", headers={"Authorization": "Bearer token"}, json={"question": "q"}
    )
    assert sample.status_code == 503
    body = sample.json()["error"]
    assert body["code"] in RETRYABLE_CODES
    assert body["retryable"] is True


def test_the_mapping_exists_in_exactly_one_place() -> None:
    """No second copy: that is how the first one drifted.

    Behaviour tests cannot see a *new* copy that happens to be correct today;
    this can. It also fails if a caller dependency stops delegating, which would
    silently create a second implementation of the same decision.
    """
    source = (SOURCE_ROOT / API_FILE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    copies = [
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == SHARED
    ]
    assert len(copies) == 1, f"{SHARED} is defined {len(copies)} times: {copies}"
    offenders: list[str] = []
    for name in CALLER_DEPENDENCIES:
        function = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
        )
        delegates = any(
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == SHARED
            for node in ast.walk(function)
        )
        if not delegates:
            offenders.append(f"{API_FILE}:{function.lineno} {name} does not delegate to {SHARED}")
        if "CallerAuthError" in ast.unparse(function):
            offenders.append(f"{API_FILE}:{function.lineno} {name} maps CallerAuthError itself")
    assert offenders == [], "; ".join(offenders)
