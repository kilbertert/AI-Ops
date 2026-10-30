"""One order-authorization check, three explicit answers (#489).

Seven call sites fetched the same `bool`, and the `bool` erased a distinction
the log and the metric could no longer express: an authorizer that *failed* (a
down directory, an unreachable source, a defect in the guard) and an ordinary
"this caller does not own this order" were both `False`. A live authorization
outage therefore looked exactly like a batch of ordinary misses — which is the
one thing an operator must be able to tell apart.

Two things are pinned here:

* **the verdicts** — `OWNED` / `NOT_OWNED` / `UNAVAILABLE`, and that the third
  one is both logged and counted;
* **the behaviour did not move** — five surfaces still refuse with their own
  statuses, and two still fall back silently to the plain answer. That split is
  a product decision each surface made; this change only stopped them from
  re-deciding what the authorizer's answer means.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import NOT_OWNED, OWNED, UNAVAILABLE, create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.identity.caller_auth import CALLER_AUTH_UNAVAILABLE, CallerAuthError
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
API_FILE = "gateway_api.py"
#: The one guard. No call site may reach `can_access` directly any more.
GUARD = "_order_authorization"
LOGGER_NAME = "aiops.gateway"
ORDER = "2096164064667852801"
SCOPE = "self"


class _Runtime:
    def shutdown(self) -> None:
        pass

    def __getattr__(self, name: str):
        if name == "record_route_metric":
            raise AttributeError(name)

        def boom(*args: object, **kwargs: object):
            del args, kwargs
            raise ValueError("runtime is down")

        return boom


class _Caller:
    def resolve(self, token: str, **kwargs: object) -> ScopeContext:
        del token, kwargs
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type=SCOPE),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read", "aiops:diagnoses:write", "aiops:faq:read"}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        del c_user_id, tenant_id
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        del b_user_id, tenant_id
        return ()


class _Authorizer:
    """Owns nothing by default; can be told to fail instead of answering."""

    def __init__(self, *, owned: bool = False, fails: bool = False) -> None:
        self.owned = owned
        self.fails = fails
        self.calls = 0

    def can_access(self, context: object, order_no: str) -> bool:
        del context, order_no
        self.calls += 1
        if self.fails:
            raise CallerAuthError("directory down", code=CALLER_AUTH_UNAVAILABLE, retryable=True)
        return self.owned


class _Metrics:
    """Records what the runtime would have recorded."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record_route_metric(self, caller, route_type, outcome, **kwargs) -> None:
        del caller
        self.rows.append((route_type, outcome, kwargs.get("error_code") or ""))


def _client(tmp_path: Path, authorizer: _Authorizer, metrics: _Metrics | None = None) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    runtime = _Runtime()
    if metrics is not None:
        runtime.record_route_metric = metrics.record_route_metric  # type: ignore[attr-defined]
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=runtime,  # type: ignore[arg-type]
            caller_resolver=_Caller(),  # type: ignore[arg-type]
            order_authorizer=authorizer,  # type: ignore[arg-type]
            platform_resolver=PlatformIdentityResolver(_Directory()),  # type: ignore[arg-type]
            faq_catalog=FAQCatalog.bundled(),
        )
    )


def test_the_verdicts_are_three_distinct_values() -> None:
    """Not a `bool`, and not two names for the same thing.

    The distinction is the point: a caller that collapses `UNAVAILABLE` into
    `NOT_OWNED` would reopen exactly the blind spot this ticket closes.
    """
    assert len({OWNED, NOT_OWNED, UNAVAILABLE}) == 3


def test_an_ordinary_miss_is_not_recorded_as_a_failure(tmp_path: Path, caplog) -> None:
    """Ownership being absent is normal traffic, not an incident.

    If this logged, the warning would fire on ordinary misses and drown the case
    the warning exists for.
    """
    metrics = _Metrics()
    client = _client(tmp_path, _Authorizer(owned=False), metrics)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = client.get(
            f"/v1/orders/{ORDER}/access",
            headers={"Authorization": "Bearer token"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert "order authorization unavailable" not in caplog.text
    assert metrics.rows == []


def test_a_failed_check_is_logged_and_counted(tmp_path: Path, caplog) -> None:
    """The whole point: an outage is distinguishable from a batch of misses.

    Asserts the record name as well as the text — a wrong logger name still
    propagates to caplog's root handler, so naming a logger that does not exist
    would pass while claiming to pin this one.
    """
    metrics = _Metrics()
    client = _client(tmp_path, _Authorizer(fails=True), metrics)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = client.get(
            f"/v1/orders/{ORDER}/access",
            headers={"Authorization": "Bearer token"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ORDER_AUTHORIZATION_UNAVAILABLE"
    # The warning carries the same code the metric and the contract use, so one
    # grep finds all three.
    warnings = [r for r in caplog.records if "ORDER_AUTHORIZATION_UNAVAILABLE" in r.getMessage()]
    assert warnings, caplog.text
    assert warnings[0].name == LOGGER_NAME, warnings[0].name
    # The row is its own bucket, not `diagnosis`: a failed precheck is component
    # health, and counting it as a diagnosis run reported runs that never
    # happened — including on surfaces that start no diagnosis at all.
    assert metrics.rows == [("order_auth", "failed", "ORDER_AUTHORIZATION_UNAVAILABLE")], metrics.rows


def test_no_call_site_reaches_the_authorizer_directly() -> None:
    """One guard, seven call sites — checked at the source.

    The defect was a *missing* distinction, not a wrong answer: every call site
    behaved correctly. So the rule is structural — `can_access` is called from
    the guard and nowhere else, which is also what keeps a future call site from
    re-inventing the `bool`.
    """
    tree = ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "attr", "") != "can_access":
            continue
        # Its own function is the guard.
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        cur: ast.AST | None = node
        owner = "<module>"
        while cur is not None and cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.FunctionDef):
                owner = cur.name
                break
        if owner != GUARD:
            offenders.append(f"{API_FILE}:{node.lineno} {owner} calls can_access directly")
    assert offenders == [], "; ".join(offenders)
    # A guard that nothing calls would satisfy the check above.
    called = {getattr(inner.func, "id", "") for inner in ast.walk(tree) if isinstance(inner, ast.Call)}
    assert GUARD in called, f"nothing calls {GUARD}"
