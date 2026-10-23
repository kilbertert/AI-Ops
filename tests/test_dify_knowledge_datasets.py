"""`sync-knowledge-datasets`：每个 RAGFlow 库 → Dify 一条 external dataset（#1001）。

判据对着三个"看起来做了、其实没做成"的形状，每一个都在 Dify 1.17.1 的源码里核过：

1. **建的时候带 permission 是做不到的。** ``ExternalDatasetCreatePayload`` 没有这个字段，
   落库默认 ``only_me`` —— 只对 maintainer 可见，编排页里选不到。所以"建完再授权"这一步
   **必须**发生；只建不授权的实现会在这里红。
2. **PATCH 缺外部字段就是 500。** ``_update_external_dataset`` 对
   ``external_knowledge_id`` / ``external_knowledge_api_id`` 都 ``raise ValueError``。
   只发 ``{"permission": …}`` 的实现在这里红。
3. **幂等键是 knowledge_id，不是名字。** 用名字做键，第二次同步会撞上自己建的 dataset。

桩 :class:`_Dify` 按**路径**分派，并记下每个请求的 body —— 断言的是"发出去的是哪一个
body"，因为这三个坑全都只体现在 body 里，状态码全是 2xx。
"""

from __future__ import annotations

from typing import Any

import pytest

from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify_knowledge_datasets import (
    NAME_MAX_LENGTH,
    PERMISSION_ALL_TEAM,
    Library,
    SyncError,
    libraries_for,
    plan,
    run,
)

TENANT = "1942105476598861824"
WORKSPACE = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"
API_ID = "af41a28b-dd3b-4b7b-8155-0930d526a75a"


class _Console:
    """扮演 ``DifyConsoleAdmin`` 的三个方法，**记录发出去的 body**。

    不是 mock：它按真实路径前缀分派，和 :class:`DifyConsoleAdmin` 的方法名一样，所以
    被测的 :func:`run` 不知道自己拿的是桩还是真客户端。
    """

    def __init__(self, datasets: list[dict[str, Any]] | None = None) -> None:
        self.datasets = list(datasets or [])
        self.calls: list[dict[str, Any]] = []
        self._next = 0

    # -- 与 DifyConsoleAdmin 同名 ------------------------------------------
    def list_datasets(self, workspace_id: str) -> list[dict[str, Any]]:
        self.calls.append({"op": "list", "workspace": workspace_id})
        return list(self.datasets)

    def create_external_dataset(
        self,
        workspace_id: str,
        *,
        name: str,
        external_knowledge_id: str,
        external_knowledge_api_id: str,
    ) -> str:
        body = {
            "name": name,
            "external_knowledge_id": external_knowledge_id,
            "external_knowledge_api_id": external_knowledge_api_id,
        }
        self.calls.append({"op": "create", "body": body})
        self._next += 1
        dataset_id = f"ds-{self._next}"
        # **Dify 的真实行为**：创建不落 permission，默认 only_me。
        self.datasets.append(
            {
                "id": dataset_id,
                "name": name,
                "permission": "only_me",
                "external_knowledge_info": {"external_knowledge_id": external_knowledge_id},
            }
        )
        return dataset_id

    def update_external_dataset(
        self,
        workspace_id: str,
        dataset_id: str,
        *,
        name: str,
        external_knowledge_id: str,
        external_knowledge_api_id: str,
        permission: str = PERMISSION_ALL_TEAM,
        external_retrieval_model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "permission": permission,
            "external_knowledge_id": external_knowledge_id,
            "external_knowledge_api_id": external_knowledge_api_id,
        }
        if external_retrieval_model is not None:
            body["external_retrieval_model"] = external_retrieval_model
        self.calls.append({"op": "update", "dataset_id": dataset_id, "body": body})
        for item in self.datasets:
            if item["id"] == dataset_id:
                item["name"] = name
                item["permission"] = permission
                if external_retrieval_model is not None:
                    item["external_retrieval_model"] = external_retrieval_model
        return {}


_READY_MODEL = {"top_k": 4, "score_threshold": 0.8, "score_threshold_enabled": False}


def _lib(dataset_id: str, name: str) -> Library:
    return Library(dataset_id=dataset_id, name=name, knowledge_id=f"tenant-{TENANT}:{dataset_id}")


def _existing(
    dataset_id: str,
    name: str,
    *,
    permission: str,
    ragflow: str = "kb-a",
    kid: str = "",
    retrieval_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Dify dataset as the list route returns it.

    ``ragflow`` is the RAGFlow library it points at — **not** derived from
    ``dataset_id``: those are two different id spaces, and a helper that
    conflated them would make "ours" and "someone else's" look the same.
    """
    return {
        "id": dataset_id,
        "name": name,
        "permission": permission,
        "external_retrieval_model": retrieval_model,
        "external_knowledge_info": {
            "external_knowledge_id": kid or f"tenant-{TENANT}:{ragflow}",
        },
    }


# ── 建：两步，缺一步就不可见 ───────────────────────────────────────────────


def test_creating_also_grants_team_visibility(tmp_path: Any = None) -> None:
    """建完必须紧跟一次 PATCH —— 否则那条 dataset 只有创建者看得见。

    这是编排页「未找到知识库」的直接成因：Dify 的创建接口**不接受** permission，
    落库默认 ``only_me``。
    """
    console = _Console()

    outcome = run(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "客服手册")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=False,
    )

    ops = [call["op"] for call in console.calls]
    assert ops == ["list", "create", "update"], ops
    create = next(c for c in console.calls if c["op"] == "create")
    assert create["body"] == {
        "name": "客服手册",
        "external_knowledge_id": f"tenant-{TENANT}:kb-a",
        "external_knowledge_api_id": API_ID,
    }
    assert "permission" not in create["body"], "创建接口不接受 permission，带了会被 422"
    patch = next(c for c in console.calls if c["op"] == "update")
    assert patch["body"]["permission"] == PERMISSION_ALL_TEAM
    assert patch["body"]["external_retrieval_model"]["top_k"] == 4, (
        "创建接口不写 retrieval_model；不补它，Dify 每次检索都 AttributeError"
    )
    assert outcome["applied"][0]["permission"] == PERMISSION_ALL_TEAM
    assert console.datasets[0]["permission"] == PERMISSION_ALL_TEAM


def test_patch_carries_both_external_fields() -> None:
    """PATCH 必须带全两个外部字段 —— Dify 对缺任一都是 ValueError（不是"保持不变"）。"""
    console = _Console(datasets=[_existing("ds-1", "客服手册", permission="only_me")])

    run(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "客服手册")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=False,
    )

    patch = next(c for c in console.calls if c["op"] == "update")
    assert patch["body"]["external_knowledge_id"] == f"tenant-{TENANT}:kb-a"
    assert patch["body"]["external_knowledge_api_id"] == API_ID


# ── 幂等 ────────────────────────────────────────────────────────────────


def test_running_twice_creates_nothing_the_second_time() -> None:
    console = _Console()
    kwargs = dict(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "A"), _lib("kb-b", "B")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=False,
    )

    run(**kwargs)
    writes_first = [c for c in console.calls if c["op"] in {"create", "update"}]
    assert len(writes_first) == 4  # 2 × (create + grant)

    console.calls.clear()
    outcome = run(**kwargs)
    assert [c for c in console.calls if c["op"] in {"create", "update"}] == []
    assert outcome["applied"] == []
    assert len(outcome["unchanged"]) == 2


def test_the_key_is_the_knowledge_id_not_the_name() -> None:
    """同名的另一条 dataset 不该被当成"我们这条"。

    若实现按名字查重，这条会把它认成自己，于是永远不改 —— 而那个 id 根本不是我们发的。
    """
    console = _Console(
        datasets=[
            _existing("ds-other", "客服手册", permission="all_team_members", kid="somebody-else"),
        ]
    )

    with pytest.raises(SyncError) as caught:
        run(
            workspace_id=WORKSPACE,
            libraries=[_lib("kb-a", "客服手册")],
            console=console,
            external_knowledge_api_id=API_ID,
            dry_run=False,
        )

    assert caught.value.code == "DIFY_DATASET_NAME_TAKEN"
    assert [c for c in console.calls if c["op"] in {"create", "update"}] == []


def test_renaming_keeps_the_id_and_never_creates() -> None:
    console = _Console(
        datasets=[_existing("ds-1", "旧名字", permission=PERMISSION_ALL_TEAM)],
    )

    run(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "新名字")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=False,
    )

    assert [c["op"] for c in console.calls if c["op"] != "list"] == ["update"]
    patch = console.calls[-1]
    assert patch["dataset_id"] == "ds-1"
    assert patch["body"]["name"] == "新名字"


# ── 不删 ────────────────────────────────────────────────────────────────


def test_a_library_gone_upstream_is_reported_and_never_deleted() -> None:
    console = _Console(
        datasets=[
            _existing("ds-1", "还在", permission=PERMISSION_ALL_TEAM, retrieval_model=_READY_MODEL),
            _existing(
                "ds-2", "已删", permission=PERMISSION_ALL_TEAM, ragflow="kb-2", retrieval_model=_READY_MODEL
            ),
        ]
    )

    outcome = run(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "还在")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=False,
    )

    assert outcome["orphans"] == [f"tenant-{TENANT}:kb-2"]
    assert [c for c in console.calls if c["op"] in {"create", "update", "delete"}] == []


# ── dry-run ─────────────────────────────────────────────────────────────


def test_dry_run_writes_nothing_but_plans_the_same_thing() -> None:
    console = _Console()

    planned = run(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "A")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=True,
    )
    assert planned["planned"] == [
        {"kind": "create", "knowledge_id": f"tenant-{TENANT}:kb-a", "name": "A", "dataset_id": ""}
    ]

    applied = run(
        workspace_id=WORKSPACE,
        libraries=[_lib("kb-a", "A")],
        console=console,
        external_knowledge_api_id=API_ID,
        dry_run=False,
    )
    assert [item["kind"] for item in applied["applied"]] == ["create"]


# ── 名字上限：取更严的那个 ─────────────────────────────────────────────────


def test_a_name_too_long_for_the_update_route_is_refused_before_creating() -> None:
    """创建允许 100 字节，**更新只允许 40**。用 100 去建，紧接着的授权 PATCH 会 422 ——
    库里留下一条谁都看不见的 dataset，而错误只说"名字太长"。
    """
    console = _Console()
    long_name = "库" * 20  # 60 字节：创建能过，更新不能

    with pytest.raises(SyncError) as caught:
        run(
            workspace_id=WORKSPACE,
            libraries=[_lib("kb-a", long_name)],
            console=console,
            external_knowledge_api_id=API_ID,
            dry_run=False,
        )

    assert caught.value.code == "DIFY_DATASET_NAME_TOO_LONG"
    assert [c for c in console.calls if c["op"] != "list"] == []
    assert len(long_name.encode()) > NAME_MAX_LENGTH


# ── 派生：只有一处 ──────────────────────────────────────────────────────


def test_the_knowledge_id_is_derived_by_the_shared_rule() -> None:
    """同步写的 id 必须就是检索认的那个 —— 两处各算一遍就会出现"同步成功、检索 404"。"""
    from aiops_diagnostics.knowledge_sources import auto_knowledge_id

    libraries = libraries_for(TENANT, [{"knowledge_base_id": "kb-a", "name": "A"}])

    assert libraries[0].knowledge_id == auto_knowledge_id(TENANT, "kb-a")
    assert libraries[0].knowledge_id == f"tenant-{TENANT}:kb-a"


def test_plan_is_pure_and_reports_a_permission_fix() -> None:
    decision = plan(
        [_lib("kb-a", "A")],
        [_existing("ds-1", "A", permission="only_me")],
    )

    assert [a.kind for a in decision.actions] == ["fix_permission"]
    assert decision.writes[0].dataset_id == "ds-1"


def test_a_dataset_with_no_retrieval_model_is_repaired() -> None:
    """#1001 建出来的那批就是这个形状：可见、能选，但**一检索就崩**。

    所以"看起来已经对"不够 —— ``plan`` 必须把它认成待修，否则重跑同步永远不会修好它。
    """
    decision = plan(
        [_lib("kb-a", "A")],
        [_existing("ds-1", "A", permission=PERMISSION_ALL_TEAM, retrieval_model=None)],
    )

    assert [a.kind for a in decision.actions] == ["fix_retrieval_model"]


def test_a_fully_correct_dataset_needs_no_write() -> None:
    decision = plan(
        [_lib("kb-a", "A")],
        [
            _existing(
                "ds-1",
                "A",
                permission=PERMISSION_ALL_TEAM,
                retrieval_model={"top_k": 4, "score_threshold": 0.8, "score_threshold_enabled": False},
            )
        ],
    )

    assert [a.kind for a in decision.actions] == ["ok"]
    assert decision.writes == ()


# ── 真实客户端的那两个方法（桩 HTTP，断言 body）──────────────────────────


def _client(tmp_path: Any) -> tuple[DifyConsoleAdmin, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    class _Recorder(DifyConsoleAdmin):
        def _call(self, workspace_id: str, path: str, *, method: str = "GET", body: Any = None, **_kw: Any):
            seen.append({"path": path, "method": method, "body": body})
            if method == "POST":
                return {"id": "ds-new"}
            if method == "GET":
                return {"data": [], "has_more": False}
            return {}

    return _Recorder(ConsoleApiSettings("http://dify.test", "key")), seen


def test_client_create_posts_the_payload_without_permission() -> None:
    client, seen = _client(None)

    dataset_id = client.create_external_dataset(
        WORKSPACE, name="A", external_knowledge_id="tenant-T:kb-a", external_knowledge_api_id=API_ID
    )

    assert dataset_id == "ds-new"
    assert seen[-1]["path"] == "/console/api/datasets/external"
    assert seen[-1]["body"] == {
        "name": "A",
        "external_knowledge_id": "tenant-T:kb-a",
        "external_knowledge_api_id": API_ID,
    }


def test_client_update_sets_all_team_and_both_external_fields() -> None:
    client, seen = _client(None)

    client.update_external_dataset(
        WORKSPACE,
        "ds-1",
        name="A",
        external_knowledge_id="tenant-T:kb-a",
        external_knowledge_api_id=API_ID,
    )

    assert seen[-1]["method"] == "PATCH"
    assert seen[-1]["path"] == "/console/api/datasets/ds-1"
    assert seen[-1]["body"] == {
        "name": "A",
        "permission": PERMISSION_ALL_TEAM,
        "external_knowledge_id": "tenant-T:kb-a",
        "external_knowledge_api_id": API_ID,
    }


def test_client_update_can_carry_the_retrieval_model() -> None:
    """``external_retrieval_model`` 是**检索能不能跑**的前提（#1002 真机抓到）。

    创建接口不写它 ⇒ 落库 NULL ⇒ Dify 检索时直接 ``.get`` 一个 None ⇒ 每次检索
    AttributeError，被吞成"没有结果"。所以 PATCH 必须带。
    """
    client, seen = _client(None)

    client.update_external_dataset(
        WORKSPACE,
        "ds-1",
        name="A",
        external_knowledge_id="tenant-T:kb-a",
        external_knowledge_api_id=API_ID,
        external_retrieval_model={"top_k": 4, "score_threshold": 0.8, "score_threshold_enabled": False},
    )

    assert seen[-1]["body"]["external_retrieval_model"] == {
        "top_k": 4,
        "score_threshold": 0.8,
        "score_threshold_enabled": False,
    }


def test_client_list_asks_for_include_all() -> None:
    """不带 ``include_all`` 就看不见 ``only_me`` 的 dataset ⇒ 每次跑都再建一条。"""
    client, seen = _client(None)

    client.list_datasets(WORKSPACE)

    assert "include_all=true" in seen[-1]["path"]
