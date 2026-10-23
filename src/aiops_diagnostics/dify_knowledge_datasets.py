"""把一个 RAGFlow 租户名下的每个库，同步成 Dify 里的一条 external dataset（#1001 / PRD #999）。

## 为什么要有这一步

#1000 让 `knowledge_id` 能**指向单个库**（`tenant-<tenant>:<dataset_id>`）—— 那是检索侧
的一半。Dify 的编排页上，"一个知识库"就是**一条 dataset**；没有那条 dataset，运营在智能体
配置里根本选不到，库级 id 也就没人用得上。

## 三个决定，每一个都是对着 Dify 1.17.1 的源码定的

1. **`permission` 不能在创建时设。** ``ExternalDatasetCreatePayload`` 只有
   `external_knowledge_api_id` / `external_knowledge_id` / `name` / `description` /
   `external_retrieval_model`，**没有 `permission`**；`create_external_dataset` 建
   `Dataset(...)` 时也不传它，落库默认 ``only_me``。而 ``only_me`` 只对 maintainer 可见
   （``DatasetService.get_datasets`` 的 legacy 分支）—— 这正是编排页「未找到知识库」的成因。
   所以：建完**必须**再 PATCH 一次。

2. **PATCH 必须回填两个外部字段。** ``_update_external_dataset`` 里
   ``external_knowledge_id`` / ``external_knowledge_api_id`` 缺任一都是 ``ValueError``，
   不是"保持不变"。只发 `{"permission": ...}` 会 500。

3. **幂等键是 ``(workspace, external_knowledge_id)``，不是名字。** ``datasets`` 表**没有**
   名字唯一约束，只有服务层在建/改时查一次同租户同名。用名字做键，第二次同步会撞上
   "已经有一个叫这个的"并留下一个空档；用 id 做键，名字只是**可被纠正的显示值**。

## 两条硬约束

- **名字上限取 40，不是 100。** 创建接口允许 100，但**更新**接口的
  ``DatasetUpdatePayload`` 只允许 40。用 100 去建，紧接着的授权 PATCH 会 422 —— 一条谁也
  看不见的 dataset 留在那儿，而错误说"名字太长"。所以创建前就按 40 拒绝。
- **不删任何 dataset。** RAGFlow 里删掉的库这里只报告：删 dataset 不可逆，且删一条正被
  智能体引用的库会静默破坏那些 app。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Dify 的权限取值（``models/enums.py`` ``DatasetPermissionEnum.ALL_TEAM``）。**不是**我们的
#: 枚举 —— 拼错不会被类型检查发现，只会被 Dify 的 Pydantic 拒成 422。
PERMISSION_ALL_TEAM = "all_team_members"

#: 名字上限：取**两个接口里更严的那个**（更新 40 / 创建 100）。
NAME_MAX_LENGTH = 40

#: external dataset 的检索参数。**必须显式写进每一条 PATCH**，理由见
#: :func:`update_external_dataset` 的文档：Dify 对 ``retrieval_model`` 直接 ``.get``，
#: 而创建接口不写它 —— 不补就是 SQL NULL，每一次检索都炸，且被吞成"没结果"。
#: 取值与 Dify UI 的默认一致（``DATASET_DEFAULT``：top_k 4、阈值 0.8），
#: 让"我们同步的"与"人点过保存的"是同一个状态。
DEFAULT_EXTERNAL_RETRIEVAL_MODEL = {
    "top_k": 4,
    "score_threshold": 0.8,
    "score_threshold_enabled": False,
}


class SyncError(RuntimeError):
    """一步失败，带一个可区分的码与一句能行动的话。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Library:
    """RAGFlow 侧的一个库。"""

    dataset_id: str
    name: str
    #: 检索侧认的那个串（#1000 的规则派生，与 ``knowledge_sources`` 共用一处）。
    knowledge_id: str


@dataclass(frozen=True, slots=True)
class Action:
    """一条要发生的事。``kind`` 决定调用方发哪个请求。"""

    kind: str  # create | rename | fix_permission | fix_retrieval_model | ok
    knowledge_id: str
    name: str
    dataset_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "knowledge_id": self.knowledge_id,
            "name": self.name,
            "dataset_id": self.dataset_id,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    actions: tuple[Action, ...]
    #: RAGFlow 里已经不存在的库 —— **只在报告里**，绝不自动删 Dify 的那条 dataset。
    orphans: tuple[str, ...]

    @property
    def writes(self) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if a.kind != "ok")

    def as_dict(self) -> dict[str, Any]:
        return {"actions": [a.as_dict() for a in self.actions], "orphans": list(self.orphans)}


def libraries_for(tenant_id: str, rows: Iterable[Mapping[str, Any]]) -> list[Library]:
    """kb-service 的清单行 → :class:`Library`。

    ``knowledge_id`` 从这里派生、**只此一处**，且与检索侧共用 ``auto_knowledge_id`` ——
    两处各算一遍就会出现"同步成功、检索 404"。
    """
    from aiops_diagnostics.knowledge_sources import auto_knowledge_id

    out: list[Library] = []
    for row in rows:
        dataset_id = str(row.get("knowledge_base_id") or row.get("id") or "")
        if not dataset_id:
            continue
        out.append(
            Library(
                dataset_id=dataset_id,
                name=str(row.get("name") or "") or dataset_id,
                knowledge_id=auto_knowledge_id(tenant_id, dataset_id),
            )
        )
    return out


def _too_long(name: str) -> bool:
    return len(name.encode("utf-8")) > NAME_MAX_LENGTH


def check_names(libraries: Iterable[Library]) -> None:
    """名字在**创建之前**就按最严的那个上限拒掉。

    放在这里而不是让 Dify 回 422：那时 dataset 已经建出来了（创建接口允许 100），紧接着的
    授权 PATCH 失败 —— 库里留下一条 owner 之外谁都看不见的 dataset，而错误只说"名字太长"。
    """
    bad = [lib for lib in libraries if _too_long(lib.name)]
    if bad:
        detail = "、".join(f"{lib.name!r}({len(lib.name.encode())})" for lib in bad[:5])
        raise SyncError(
            "DIFY_DATASET_NAME_TOO_LONG",
            f"这些库名超过 {NAME_MAX_LENGTH} 字节，Dify 的更新接口不收：{detail}。"
            "先在 RAGFlow 里改名，或用 --name 覆盖（见 --help）",
        )


def _retrieval_model_never_written(reported: Any) -> bool:
    """这条 dataset 的 ``retrieval_model`` 是不是"从没被写过"。

    **列表接口的回应不能当证据用。** ``DatasetExternalRetrievalModelResponse`` 是
    ``dataset.external_retrieval_model`` 这个**属性**的投影，而那个属性是
    ``self.retrieval_model or {"top_k": 2, "score_threshold": 0.0}`` —— 列是 SQL NULL 时
    它照样回一个"看起来正常"的对象。所以在 41 上真实看到的形状是：

        {"top_k": 2, "score_threshold": 0.0, "score_threshold_enabled": null}

    这是"从没写过"的指纹：``score_threshold_enabled`` 在兜底字典里**不存在**，只有
    真被写过才会有值。用 ``not reported`` 判会**永远为假**（它总有个对象），
    于是这条修复永远不触发 —— 而"看起来写了修复"比没写更糟。
    """
    if not isinstance(reported, Mapping):
        return True
    return reported.get("score_threshold_enabled") is None


def plan(libraries: Iterable[Library], existing: Iterable[Mapping[str, Any]]) -> Plan:
    """只算不改：这一批库对着 Dify 的现状会发生什么。

    纯函数。``existing`` 是 ``GET /console/api/datasets`` 的 items（每条带 ``id`` /
    ``name`` / ``permission`` / ``external_knowledge_info.external_knowledge_id``）。
    """
    by_knowledge_id: dict[str, Mapping[str, Any]] = {}
    taken_names: dict[str, str] = {}
    for item in existing:
        knowledge_id = str((item.get("external_knowledge_info") or {}).get("external_knowledge_id") or "")
        name = str(item.get("name") or "")
        if knowledge_id:
            by_knowledge_id[knowledge_id] = item
        if name:
            taken_names[name] = str(item.get("id") or "")

    actions: list[Action] = []
    for lib in libraries:
        item = by_knowledge_id.get(lib.knowledge_id)
        if item is None:
            owner = taken_names.get(lib.name)
            if owner is not None:
                # 同租户同名会被服务层拒。这条 dataset **不是**我们这个库的（否则上面的
                # by_knowledge_id 会命中），所以不能改它、也不能跳过 —— 报出来让人决定。
                raise SyncError(
                    "DIFY_DATASET_NAME_TAKEN",
                    f"{lib.knowledge_id} 想叫 {lib.name!r}，但这个名字已被 workspace 里的另一条 "
                    f"dataset（{owner}）占用。Dify 不允许同租户同名：先处理那一条，或用 --name 覆盖",
                )
            taken_names[lib.name] = ""
            actions.append(Action("create", lib.knowledge_id, lib.name))
            continue
        dataset_id = str(item.get("id") or "")
        current_name = str(item.get("name") or "")
        if current_name != lib.name:
            holder = taken_names.get(lib.name)
            if holder is not None and holder != dataset_id:
                raise SyncError(
                    "DIFY_DATASET_NAME_TAKEN",
                    f"{lib.knowledge_id} 要改名叫 {lib.name!r}，但这个名字已被 workspace 里的另一条 "
                    f"dataset（{holder}）占用。Dify 不允许同租户同名：先处理那一条，"
                    f"或在 RAGFlow 里给这个库换一个名字",
                )
            taken_names[lib.name] = dataset_id
            actions.append(Action("rename", lib.knowledge_id, lib.name, dataset_id))
        elif str(item.get("permission") or "") != PERMISSION_ALL_TEAM:
            actions.append(Action("fix_permission", lib.knowledge_id, lib.name, dataset_id))
        elif _retrieval_model_never_written(item.get("external_retrieval_model")):
            # 已存在、已可见，但**检索跑不起来**：Dify 的检索侧对
            # ``dataset.retrieval_model`` 直接 ``.get``，而创建接口从不写这一列。
            # 这正是 #1001 建出来的那批的形状，所以修它必须走同一条 PATCH。
            actions.append(Action("fix_retrieval_model", lib.knowledge_id, lib.name, dataset_id))
        else:
            actions.append(Action("ok", lib.knowledge_id, lib.name, dataset_id))

    orphans = tuple(sorted(set(by_knowledge_id) - {lib.knowledge_id for lib in libraries}))
    return Plan(actions=tuple(actions), orphans=orphans)


def run(
    *,
    workspace_id: str,
    libraries: list[Library],
    console: Any,
    external_knowledge_api_id: str,
    dry_run: bool,
    log: Any = lambda _line: None,
) -> dict[str, Any]:
    """把 ``plan()`` 的决定落到 Dify。

    ``dry_run`` 时**一个写请求都不发**；它返回的 ``planned`` 与真跑的 ``applied`` 形状相同，
    所以"看一遍再跑"读的是同一份东西。
    """
    check_names(libraries)
    existing = console.list_datasets(workspace_id)
    decision = plan(libraries, existing)
    if dry_run:
        log("dry-run：没有发任何写请求")
        return {
            "workspace_id": workspace_id,
            "dry_run": True,
            "planned": [a.as_dict() for a in decision.writes],
            "unchanged": [a.as_dict() for a in decision.actions if a.kind == "ok"],
            "orphans": list(decision.orphans),
        }

    applied: list[dict[str, Any]] = []
    for action in decision.writes:
        if action.kind == "create":
            dataset_id = console.create_external_dataset(
                workspace_id,
                name=action.name,
                external_knowledge_id=action.knowledge_id,
                external_knowledge_api_id=external_knowledge_api_id,
            )
            console.update_external_dataset(
                workspace_id,
                dataset_id,
                name=action.name,
                external_knowledge_id=action.knowledge_id,
                external_knowledge_api_id=external_knowledge_api_id,
                external_retrieval_model=DEFAULT_EXTERNAL_RETRIEVAL_MODEL,
            )
            applied.append(
                {
                    **action.as_dict(),
                    "dataset_id": dataset_id,
                    "permission": PERMISSION_ALL_TEAM,
                    "external_retrieval_model": DEFAULT_EXTERNAL_RETRIEVAL_MODEL,
                }
            )
            log(f"created {action.knowledge_id} → {action.name}")
        else:
            console.update_external_dataset(
                workspace_id,
                action.dataset_id,
                name=action.name,
                external_knowledge_id=action.knowledge_id,
                external_knowledge_api_id=external_knowledge_api_id,
                external_retrieval_model=DEFAULT_EXTERNAL_RETRIEVAL_MODEL,
            )
            applied.append(action.as_dict())
            log(f"{action.kind} {action.knowledge_id}")
    return {
        "workspace_id": workspace_id,
        "dry_run": False,
        "applied": applied,
        "unchanged": [a.as_dict() for a in decision.actions if a.kind == "ok"],
        "orphans": list(decision.orphans),
    }


__all__ = [
    "Action",
    "Library",
    "NAME_MAX_LENGTH",
    "PERMISSION_ALL_TEAM",
    "Plan",
    "SyncError",
    "check_names",
    "libraries_for",
    "plan",
    "run",
]
