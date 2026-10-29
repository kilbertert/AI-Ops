"""把 Dify 的外部知识库配置搬进租户自己的 workspace（#927 / PRD #923）。

## 这条命令防的是什么

一处**已经发生过三次**的错误形态：Dify 的数据集搬进了租户 workspace，而它绑定的
「外部知识库 API 模板」留在旧的管理 workspace —— 两者跨 workspace 时召回测试直接 **403**，
而界面上看是两个都在的结构。本命令搬完之后**自检**这一点，不满足就失败。

第二个：`dataset_permissions.has_permission` 是**布尔**不是枚举，且权限行的 `tenant_id`
必须与数据集**同批更新**；漏一个就是"数据集在新 workspace、权限还挂在旧 workspace"，
表现为权限检查 403。

第三个：外部知识库模板在两个 workspace 里**同名**是常态（一个租户一个）。搬完之后必须
确认"数据集绑的是本 workspace 那一份"，否则会出现"看起来都在租户 workspace 里，但绑定
指着旧的那张"。

## 它不做什么

不修改 app 的 mode、不写 `app_dataset_joins`、不动任何智能体配置。那三件都会改变生产
契约面，属于产品决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 搬完之后给重复模板加的前缀。保留可回溯，不删除。
ARCHIVED_PREFIX = "[归档] "


class MigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class MigrationReport:
    tenant_id: str
    workspace_id: str
    moved_datasets: tuple[str, ...]
    moved_apis: tuple[str, ...]
    archived_apis: tuple[str, ...]
    rebound: tuple[str, ...]
    already_correct: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "moved_datasets": list(self.moved_datasets),
            "moved_apis": list(self.moved_apis),
            "archived_apis": list(self.archived_apis),
            "rebound": list(self.rebound),
            "already_correct": self.already_correct,
        }


def plan(datasets: list[dict[str, Any]], apis: list[dict[str, Any]], *, workspace_id: str) -> dict[str, Any]:
    """Decide what would change, without changing it.

    Pure function over two snapshots so the decision — which is where the three
    traps live — can be tested without a database of any kind.
    """
    to_move_datasets = [d for d in datasets if str(d.get("tenant_id")) != workspace_id]

    # **Every** dataset's template is a candidate, not just the ones moving now.
    # A dataset can already be in the target workspace while its template stayed
    # behind — the partial-migration state this module exists to repair. Taking
    # only `moved_ids` made that state look `already_correct` while `verify()`
    # immediately rejected it.
    related_api_ids = {
        str(d["external_knowledge_api_id"]) for d in datasets if d.get("external_knowledge_api_id")
    }
    to_move_apis = [
        a for a in apis if str(a.get("id")) in related_api_ids and str(a.get("tenant_id")) != workspace_id
    ]

    # Same-named templates: after the move, a dataset must bind **the copy that
    # ends up in this workspace**. Peers therefore include (a) templates already
    # there and (b) templates scheduled to move here in this same plan — two
    # datasets each pointing at a same-named template in the old workspace would
    # otherwise both move, recreating the duplicate `verify()` rejects.
    by_name: dict[str, list[dict[str, Any]]] = {}
    for api in apis:
        by_name.setdefault(str(api.get("name") or ""), []).append(api)

    rebound: list[str] = []
    archived: list[str] = []
    surviving_moves: list[dict[str, Any]] = []
    # Survivor per name = a live copy already in the target workspace, else the
    # first one this plan would move there. `claimed_by_move` only records names
    # that HAVE a survivor — an archived-and-nothing-else name must not look
    # claimed, or two datasets binding two same-named strays would both be told
    # "rebind to it" and neither would move.
    survivor: dict[str, str] = {}
    claimed_by_move: set[str] = set()
    for api in sorted(to_move_apis, key=lambda a: str(a["id"])):
        name = str(api.get("name") or "")
        existing = [p for p in by_name.get(name, []) if str(p.get("tenant_id")) == workspace_id]
        if existing:
            survivor[name] = str(existing[0]["id"])
            archived.append(str(api["id"]))
            continue
        if name in claimed_by_move:
            archived.append(str(api["id"]))
            continue
        claimed_by_move.add(name)
        survivor[name] = str(api["id"])
        surviving_moves.append(api)

    to_move_apis = surviving_moves
    archived_ids = set(archived)

    # A dataset binds its template when that template is (or already was) in this
    # workspace. If its template is archived, it has to be pointed at the
    # survivor; if there is no survivor in this workspace, the template moves and
    # the dataset keeps pointing at it.
    for dataset in datasets:
        api_id = str(dataset.get("external_knowledge_api_id") or "")
        if not api_id:
            continue
        api = next((a for a in apis if str(a.get("id")) == api_id), None)
        if api is None or str(api.get("tenant_id")) == workspace_id:
            continue
        if api_id in archived_ids:
            target = survivor.get(str(api.get("name") or ""))
            if target is None:
                raise MigrationError(
                    "UNRESOLVABLE_DUPLICATE",
                    f"dataset {dataset['id']} binds {api_id}, which would be archived with no "
                    "surviving template of the same name in the target workspace",
                )
            rebound.append(str(dataset["id"]))
        # else: the template moves with the dataset; nothing to rebind.

    return {
        "datasets": [str(d["id"]) for d in to_move_datasets],
        "apis": [str(a["id"]) for a in to_move_apis],
        "rebound": rebound,
        "archive": archived,
        "already_correct": not to_move_datasets and not to_move_apis and not rebound and not archived,
    }


def verify(*, datasets: list[dict[str, Any]], apis: list[dict[str, Any]], workspace_id: str) -> None:
    """Post-move self-check. **Raises** instead of reporting success on a bad shape.

    Three assertions, one per trap. The third is the subtle one: a dataset can be
    in the right workspace while its binding points at a template that is not.
    """
    api_workspace = {str(a["id"]): str(a.get("tenant_id")) for a in apis}
    api_name = {str(a["id"]): str(a.get("name") or "") for a in apis}
    for dataset in datasets:
        if str(dataset.get("tenant_id")) != workspace_id:
            raise MigrationError(
                "DATASET_NOT_MOVED", f"dataset {dataset['id']} is still outside the tenant workspace"
            )
        api_id = str(dataset.get("external_knowledge_api_id") or "")
        if not api_id:
            continue
        if api_workspace.get(api_id) != workspace_id:
            raise MigrationError(
                "BINDING_CROSSES_WORKSPACES",
                f"dataset {dataset['id']} ({dataset.get('name')}) is bound to template {api_id} "
                f"which lives in another workspace — the console answers 403 for it",
            )
        # 同名重复：本 workspace 里必须只有这一份没被归档。
        name = api_name.get(api_id, "")
        live = [
            a
            for a in apis
            if str(a.get("name") or "") == name
            and str(a.get("tenant_id")) == workspace_id
            and not str(a.get("name") or "").startswith(ARCHIVED_PREFIX)
        ]
        if len(live) > 1:
            raise MigrationError(
                "AMBIGUOUS_TEMPLATE",
                f"workspace has {len(live)} live templates named {name!r}; "
                "renaming one before moving is required",
            )
