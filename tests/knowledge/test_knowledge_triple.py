"""1:1:1 核对（#927）：三项分开报，不合成一个"不完整"。

判据对着三种**修法不同**的半成品：只缺 workspace（重跑开户）、只缺 RAGFlow
（重跑开户，但它会再建一个 RAGFlow 账号）、两个都在但没校验过模型（重跑开户会补齐）。
把它们压成一个布尔，下一个人还得回头查三张表 —— 那正是这条命令要消灭的活。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops_diagnostics.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.knowledge.knowledge_triple import (
    MISSING_RAGFLOW,
    MISSING_WORKSPACE,
    UNVERIFIED_MODEL,
    WORKSPACE_DANGLING,
    check,
    render,
)

ENV = "41"
TENANT = "1942105476598861824"


@pytest.fixture()
def store(tmp_path: Path) -> GatewayStore:
    return GatewayStore(tmp_path / "gateway.db")


def _workspace(store: GatewayStore, tenant_id: str, workspace_id: str, state: str = "created") -> None:
    workspaces = DifyWorkspaces(store.path)
    workspaces.claim(tenant_id, ENV, owner="test", lease_seconds=300.0)
    workspaces.mark_created(tenant_id, ENV, workspace_id)


def _ragflow(store: GatewayStore, tenant_id: str, *, verified: bool) -> None:
    store.record_ragflow_tenant(
        tenant_id,
        ENV,
        ragflow_email="x@kb.internal",
        credential_ciphertext="enc:v1:x",
        model_checked=verified,
    )


def test_a_complete_tenant_is_ok(store: GatewayStore) -> None:
    _workspace(store, TENANT, "ws-1")
    _ragflow(store, TENANT, verified=True)

    results = check(store=store, environment=ENV, dify_workspace_names={"ws-1": "aiops-41-x"})

    assert len(results) == 1 and results[0].ok
    assert results[0].problems == ()


def test_a_tenant_with_only_a_ragflow_half_reports_the_missing_workspace(store: GatewayStore) -> None:
    _ragflow(store, TENANT, verified=True)

    results = check(store=store, environment=ENV, dify_workspace_names={})

    assert results[0].problems == (MISSING_WORKSPACE,)


def test_a_tenant_with_only_a_workspace_reports_the_missing_ragflow_half(store: GatewayStore) -> None:
    _workspace(store, TENANT, "ws-1")

    results = check(store=store, environment=ENV, dify_workspace_names={})

    assert results[0].problems == (MISSING_RAGFLOW,)


def test_an_unverified_model_is_its_own_problem_not_an_missing_half(store: GatewayStore) -> None:
    """Row present, model never proven: onboarding failed at the last step.

    Reporting this as "missing ragflow tenant" would send the operator to create
    a second account for a tenant that already has one.
    """
    _workspace(store, TENANT, "ws-1")
    _ragflow(store, TENANT, verified=False)

    results = check(store=store, environment=ENV, dify_workspace_names={"ws-1": "n"})

    assert results[0].problems == (UNVERIFIED_MODEL,)


def test_a_workspace_dify_does_not_have_is_reported_as_dangling(store: GatewayStore) -> None:
    """The mapping says ws-1; Dify's own listing does not contain it."""
    _workspace(store, TENANT, "ws-1")
    _ragflow(store, TENANT, verified=True)

    results = check(store=store, environment=ENV, dify_workspace_names={"ws-OTHER": "n"})

    assert results[0].problems == (WORKSPACE_DANGLING,)


def test_dify_not_consulted_does_not_condemn_every_tenant(store: GatewayStore) -> None:
    """An empty listing means "we did not look", not "Dify has nothing".

    Judging workspace existence from a listing we never fetched would mark every
    healthy tenant broken — worse than saying nothing about that dimension.
    """
    _workspace(store, TENANT, "ws-1")
    _ragflow(store, TENANT, verified=True)

    assert check(store=store, environment=ENV, dify_workspace_names={})[0].ok


def test_both_sides_contribute_to_the_tenant_set(store: GatewayStore) -> None:
    """A half-built tenant is exactly what this exists to surface.

    Letting either registry define the set alone would hide it: the tenant is
    absent from one of them by construction.
    """
    _workspace(store, "T-ONLY-WS", "ws-a")
    _ragflow(store, "T-ONLY-RAGFLOW", verified=True)

    found = {item.tenant_id for item in check(store=store, environment=ENV, dify_workspace_names={})}

    assert found == {"T-ONLY-WS", "T-ONLY-RAGFLOW"}


def test_render_names_each_problem(store: GatewayStore) -> None:
    _ragflow(store, TENANT, verified=True)

    text = render(check(store=store, environment=ENV, dify_workspace_names={}))

    assert MISSING_WORKSPACE in text
    assert "1 tenant(s), 1 incomplete" in text


# ---- CLI 面 ----------------------------------------------------------------


def test_check_command_exits_1_when_a_tenant_is_incomplete(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app

    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", "41")
    monkeypatch.setenv("AIOPS_GATEWAY_DATABASE_FILE", str(tmp_path / "gateway.db"))
    store = GatewayStore(tmp_path / "gateway.db")
    _ragflow(store, TENANT, verified=True)

    result = CliRunner().invoke(admin_app, ["check-knowledge-triple"])

    assert result.exit_code == 1, result.output
    assert MISSING_WORKSPACE in result.output


def test_plan_command_reports_a_plan_and_writes_nothing(monkeypatch) -> None:
    """The plan subcommand is pure: its whole job is to be safe to run repeatedly."""
    import json as _json

    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app

    datasets = _json.dumps(
        [{"id": "ds-1", "name": "kb", "tenant_id": "old", "external_knowledge_api_id": "a1"}]
    )
    apis = _json.dumps([{"id": "a1", "name": "tmpl", "tenant_id": "old"}])

    result = CliRunner().invoke(
        admin_app,
        ["plan-knowledge-migration", "--workspace", "new", "--datasets", datasets, "--apis", apis],
    )

    assert result.exit_code == 0, result.output
    assert '"ds-1"' in result.output


def test_check_command_refuses_a_database_that_does_not_exist(tmp_path: Path, monkeypatch) -> None:
    """An empty store reports zero tenants, and zero tenants exit 0.

    That is "I inspected the wrong file" presented as a clean bill of health —
    the failure mode this check must never have.
    """
    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app

    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", "41")
    missing = tmp_path / "typo.db"

    result = CliRunner().invoke(admin_app, ["check-knowledge-triple", "--db", str(missing)])

    assert result.exit_code == 2
    assert "数据库不存在" in result.output
    assert not missing.exists(), "the command must not create the file it was asked to inspect"
