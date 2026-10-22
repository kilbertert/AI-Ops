"""迁移计划与自检（#927）：三条坑各一条，外加"已经是正确的就什么都不做"。

`plan()` 是对两棵快照的纯函数 —— 三个坑都住在"决定"里，而不在 SQL 里，所以它们
可以完全不碰数据库地被测到。
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.dify_knowledge_migration import (
    ARCHIVED_PREFIX,
    MigrationError,
    plan,
    verify,
)

WS = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"
OLD_WS = "2db44904-f981-42ca-b7e2-6f35cb2afffd"


def _dataset(ds_id: str, *, tenant: str = OLD_WS, api: str = "api-old", name: str = "kb-prod") -> dict:
    return {
        "id": ds_id,
        "name": name,
        "tenant_id": tenant,
        "external_knowledge_api_id": api,
    }


def _api(api_id: str, *, tenant: str = OLD_WS, name: str = "aiops-41-knowledge-production") -> dict:
    return {"id": api_id, "name": name, "tenant_id": tenant}


def test_plan_moves_the_dataset_and_the_template_it_binds() -> None:
    datasets = [_dataset("ds-1")]
    apis = [_api("api-old")]

    result = plan(datasets, apis, workspace_id=WS)

    assert result["datasets"] == ["ds-1"]
    assert result["apis"] == ["api-old"]
    assert result["already_correct"] is False


def test_plan_rebinds_to_a_same_named_template_already_in_the_workspace() -> None:
    """The second trap: two workspaces each hold a template with the same name.

    Moving the dataset without rebinding leaves it pointing at the OLD one — the
    console then answers 403 while everything *looks* like it is in the right
    place.
    """
    datasets = [_dataset("ds-1", api="api-old")]
    apis = [_api("api-old", tenant=OLD_WS), _api("api-new", tenant=WS)]

    result = plan(datasets, apis, workspace_id=WS)

    assert result["rebound"] == ["ds-1"]
    assert result["archive"] == ["api-old"]
    assert "api-old" not in result["apis"], "the old template must not be moved, it is archived in place"


def test_an_already_correct_tenant_is_a_no_op() -> None:
    """41 is in this state today: re-running must be clean, not a second archive."""
    datasets = [_dataset("ds-1", tenant=WS, api="api-new")]
    apis = [_api("api-new", tenant=WS)]

    result = plan(datasets, apis, workspace_id=WS)

    assert result["already_correct"] is True
    assert result["datasets"] == [] and result["apis"] == [] and result["rebound"] == []


def test_verify_rejects_a_binding_that_crosses_workspaces() -> None:
    datasets = [_dataset("ds-1", tenant=WS, api="api-old")]
    apis = [_api("api-old", tenant=OLD_WS)]

    with pytest.raises(MigrationError) as caught:
        verify(datasets=datasets, apis=apis, workspace_id=WS)

    assert caught.value.code == "BINDING_CROSSES_WORKSPACES"


def test_verify_rejects_a_dataset_left_behind() -> None:
    with pytest.raises(MigrationError) as caught:
        verify(datasets=[_dataset("ds-1")], apis=[], workspace_id=WS)

    assert caught.value.code == "DATASET_NOT_MOVED"


def test_verify_rejects_two_live_templates_with_the_same_name() -> None:
    """Ambiguity is refused, not resolved by picking one — picking wrong is cross-tenant."""
    datasets = [_dataset("ds-1", tenant=WS, api="api-new")]
    apis = [_api("api-new", tenant=WS), _api("api-other", tenant=WS)]

    with pytest.raises(MigrationError) as caught:
        verify(datasets=datasets, apis=apis, workspace_id=WS)

    assert caught.value.code == "AMBIGUOUS_TEMPLATE"


def test_verify_accepts_an_archived_duplicate_beside_the_live_one() -> None:
    datasets = [_dataset("ds-1", tenant=WS, api="api-new")]
    apis = [
        _api("api-new", tenant=WS),
        _api("api-old", tenant=WS, name=f"{ARCHIVED_PREFIX}aiops-41-knowledge-production"),
    ]

    verify(datasets=datasets, apis=apis, workspace_id=WS)  # must not raise


def test_plan_repairs_a_dataset_already_moved_while_its_template_stayed_behind() -> None:
    """The partial state: dataset in the target, template in the old workspace.

    Before the fix this produced `already_correct=true` while `verify()` on the
    same snapshots raised BINDING_CROSSES_WORKSPACES — the planner said "nothing
    to do" about a shape it would then reject.
    """
    datasets = [_dataset("ds-1", tenant=WS, api="api-old")]
    apis = [_api("api-old", tenant=OLD_WS)]

    result = plan(datasets, apis, workspace_id=WS)

    assert result["already_correct"] is False
    assert result["apis"] == ["api-old"]
    # And the plan it produces must survive its own post-check.
    moved = [dict(a, tenant_id=WS) for a in apis]
    verify(datasets=datasets, apis=moved, workspace_id=WS)


def test_two_same_named_strays_do_not_both_move() -> None:
    """Two datasets each bind a same-named template in the old workspace.

    Moving both would land two live templates with one name in the target — the
    state `verify()` calls AMBIGUOUS_TEMPLATE. The plan must archive one and
    rebind its dataset, and still survive verify.
    """
    datasets = [
        _dataset("ds-1", api="api-a", name="kefu"),
        _dataset("ds-2", api="api-b", name="kefu"),
    ]
    apis = [
        _api("api-a", tenant=OLD_WS, name="aiops-41-knowledge-production"),
        _api("api-b", tenant=OLD_WS, name="aiops-41-knowledge-production"),
    ]

    result = plan(datasets, apis, workspace_id=WS)

    assert len(result["apis"]) == 1, "exactly one survivor moves"
    assert len(result["archive"]) == 1
    assert len(result["rebound"]) == 1, "the dataset bound to the archived one is repointed"

    survivor = result["apis"][0]
    moved = [dict(a, tenant_id=WS) for a in apis if a["id"] == survivor] + [
        dict(a, tenant_id=WS, name=f"{ARCHIVED_PREFIX}{a['name']}") for a in apis if a["id"] != survivor
    ]
    rebind = {ds: survivor for ds in result["rebound"]}
    fixed = [
        dict(d, tenant_id=WS, external_knowledge_api_id=rebind.get(d["id"], d["external_knowledge_api_id"]))
        for d in datasets
    ]
    verify(datasets=fixed, apis=moved, workspace_id=WS)
