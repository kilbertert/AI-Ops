"""知识源解析：**一处**决定 "Dify 发的这个字符串对应哪个租户、哪些库"（#925 / PRD #923）。

## 为什么要有这个模块

在此之前这件事有两个入口各算一遍：检索路由与清单路由各写了一段"env 登记表 + 数据库
登记表合并"的代码。两份实现今天一致，明天不一定 —— 而这个仓库里"同一规则两份实现"
已经漂过至少三次（见 `docs/development` 里的反复记录）。所以把合并规则抽出来，
两个路由都调它。

## 三层来源，优先级唯一

```
1. 人工登记表（数据库）      ← 运营在 /v1/knowledge 点的，或历史 env 行迁移来的
2. env 登记表               ← 部署时写的 `knowledge_id:tenant:kb1|kb2`
3. 自动登记（1:1:1）         ← 租户在 RAGFlow 名下有哪些库，由开户建立的那一半推出
```

**人工登记赢**，与 ADR-0048 一致：那一行是人点出来的，是较新的决定。自动登记只在
"前两层都没有这个 id"时补上，所以：

* 已经登记过的绑定在本次改造后**行为一字不变**；
* 回滚不需要恢复数据 —— 关掉自动登记，剩下的就是今天的样子；
* 跨租户共享同一个库仍然只能靠人工登记（自动登记只列本租户的）。

## 自动登记的 `knowledge_id` 规则（两种，都保留）

```
tenant-<tenant_id>                  → 该租户名下全部库（#925 的形态，逐字保留）
tenant-<tenant_id>:<dataset_id>     → 恰好那一个库（#1000）
```

为什么必须是派生而不是"开户时生成一个 UUID 存起来"：Dify 会把 `knowledge_id` **存进
外部知识库配置**。改名等于让所有已配置的 app 立刻 404。派生值在任何时候、任何进程里
算出同一个结果，所以补登记、重建库、迁移机器都不会改它。

为什么租户级还不够（#1000 的由来）：Dify 的编排页里，一个**知识库**就是一条外部
dataset —— 一条 dataset 只能指向一个 `knowledge_id`。租户级 id 把所有库合成一个源，
于是编排页上只能看到"整个租户一个库"，选不出「客服手册」和「售后流程」的区别。
一个库一条 id，才能一个库一条 dataset。

为什么带 dataset id 而不是库名：库名会变，`knowledge_id` 被 Dify 存进配置，改名就 404。
dataset id 不变。

**两种都保留**：旧 id 已经写在别人的 Dify 配置里，解析不出来就是让线上 app 立刻 404。
租户级的语义（合并全部库）因此永不改变；新形态只是把同一批库**逐个**暴露出来。

**新形态必须校验归属**：`tenant-T:KB-X` 只有在 `KB-X` 确实出现在租户 T 自己的清单里时
才可服务。否则任何人只要猜到一个 dataset id，就能借**别的**租户的名字检索它 —— 而
租户 id 是公开的（`tenant-<id>` 就在 Dify 配置里）。不在清单里 ⇒ 与"未登记"同答 404，
既不泄露库是否存在，也不泄露它属于谁。

## 调试身份

自动登记同样受调试身份的子集约束：调试凭据看不到生产租户那一项。判据与检索侧同一条，
所以选择器里出现的每一项，同一把凭据都检索得到。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from aiops_diagnostics.gateway.gateway_config import DifyKnowledgeBinding, parse_dify_knowledge_bindings

#: 自动登记的 `knowledge_id` 前缀。改它等于让所有已配置的 app 404 —— 这是约定，不是旋钮。
AUTO_PREFIX = "tenant-"
#: 租户与单个库之间的分隔符。**第一次**出现处切分，所以租户 id 里即使含冒号也不会被
#: 错切到库那一段之外（库 id 是 RAGFlow 的，不含冒号）。
AUTO_SEPARATOR = ":"


def auto_knowledge_id(tenant_id: str, dataset_id: str = "") -> str:
    """The stable name a tenant's libraries are reachable under.

    Without ``dataset_id``: the tenant-level id that merges everything the tenant
    owns (the #925 form, unchanged). With it: exactly that one library (#1000).
    """
    base = f"{AUTO_PREFIX}{tenant_id}"
    return f"{base}{AUTO_SEPARATOR}{dataset_id}" if dataset_id else base


def parse_auto_knowledge_id(knowledge_id: str) -> tuple[str, str] | None:
    """``knowledge_id`` → ``(tenant_id, dataset_id)``; ``dataset_id`` is ``""``
    for the tenant-level form. ``None`` = not an automatically derived id.

    The **only** place this string is taken apart: every route resolves through
    :meth:`KnowledgeSourceResolver.resolve`. A malformed derived id (``tenant-x:``)
    answers ``None`` — the same answer as "never registered" — so it cannot be
    used to probe what exists upstream.
    """
    if not knowledge_id.startswith(AUTO_PREFIX):
        return None
    tenant_id, separator, dataset_id = knowledge_id[len(AUTO_PREFIX) :].partition(AUTO_SEPARATOR)
    if not tenant_id:
        return None
    if separator and not dataset_id:
        return None
    return tenant_id, dataset_id


@dataclass(frozen=True, slots=True)
class KnowledgeSourceResolver:
    """Resolves ``knowledge_id`` → ``(tenant, libraries)``, once, for every route.

    Constructed per request from the pieces the routes already hold: the env
    registry string, the store's rows, and (optionally) the libraries each
    tenant owns. Everything is plain data — no I/O happens here, so a test can
    drive it without a database or a kb-service.
    """

    #: 人工登记（数据库 + env），已合并。
    manual: Mapping[str, DifyKnowledgeBinding]
    #: 租户 → 它名下 RAGFlow 库的 id。空 = 自动登记对这次请求不可用（例如 kb-service
    #: 不可达，或这个部署还没开户）。**不可用时不编造**：`tenant-<id>` 仍然解析不到，
    #: 与"这个租户没有库"是同一个答案，而不是一个假装存在的空源。
    auto_libraries: Mapping[str, tuple[str, ...]]
    #: 哪些租户允许自动登记（即开户过）。不在集合里的租户不会因为"碰巧有别处的库"
    #: 而被自动放出来。
    auto_tenants: frozenset[str]

    @classmethod
    def from_parts(
        cls,
        *,
        env_bindings: str,
        rows: Iterable[Mapping[str, object]],
        auto_libraries: Mapping[str, tuple[str, ...]] | None = None,
        auto_tenants: Iterable[str] | None = None,
    ) -> KnowledgeSourceResolver:
        merged = parse_dify_knowledge_bindings(env_bindings)
        for row in rows:
            knowledge_id = str(row["knowledge_id"])
            merged[knowledge_id] = DifyKnowledgeBinding(
                knowledge_id=knowledge_id,
                tenant_id=str(row["tenant_id"]),
                knowledge_base_ids=tuple(str(x) for x in row["knowledge_base_ids"]),  # type: ignore[union-attr]
                display_name=str(row.get("display_name") or ""),
            )
        return cls(
            manual=merged,
            auto_libraries={k: tuple(v) for k, v in (auto_libraries or {}).items()},
            auto_tenants=frozenset(auto_tenants or ()),
        )

    def resolve(
        self,
        knowledge_id: str,
        *,
        fetch_libraries: Callable[[str], tuple[str, ...] | None] | None = None,
    ) -> DifyKnowledgeBinding | None:
        """The binding for this id, or None when nothing may serve it.

        Manual first, and **before any I/O**: a row written by a human (or an env
        line that predates the console) is the newer decision and must not be
        shadowed by a derived name that happens to collide — nor made to wait
        behind an unrelated tenant's upstream listing.

        ``fetch_libraries`` is how a caller gets the libraries for **the one
        tenant this id names**, instead of enumerating every onboarded tenant.
        Enumerating on the retrieval path would make one tenant's slow upstream
        add its timeout to another tenant's unrelated request; the listing route
        is the only caller that legitimately wants all of them.
        """
        manual = self.manual.get(knowledge_id)
        if manual is not None:
            return manual
        parsed = parse_auto_knowledge_id(knowledge_id)
        if parsed is None:
            return None
        tenant_id, dataset_id = parsed
        if tenant_id not in self.auto_tenants:
            return None
        libraries = self.auto_libraries.get(tenant_id)
        if libraries is None and fetch_libraries is not None:
            libraries = fetch_libraries(tenant_id)
        libraries = libraries or ()
        if dataset_id:
            # Ownership, checked against **this** tenant's own listing. A dataset
            # id guessed by a caller must not borrow another tenant's name: the
            # tenant ids are public (they are in Dify's config as `tenant-<id>`),
            # so the guess is cheap and this is the line that has to stop it.
            if dataset_id not in libraries:
                return None
            libraries = (dataset_id,)
        if not libraries:
            # 开户过但这个刻没有库：**不是**一个可服务的源。返回一个空库列表的绑定会让
            # 检索回 200 + 空 records，与"没命中"无法区分 —— 那是本仓反复要消灭的
            # 假陈述。这里让它与"未登记"同答（404）。
            return None
        return DifyKnowledgeBinding(
            knowledge_id=knowledge_id,
            tenant_id=tenant_id,
            knowledge_base_ids=libraries,
            display_name="",
        )

    def visible(
        self,
        identity: object,
        *,
        fetch_libraries: Callable[[str], tuple[str, ...] | None] | None = None,
    ) -> list[DifyKnowledgeBinding]:
        """Every binding this identity may use, sorted by id.

        This is the **only** caller that enumerates: the picker genuinely wants
        every source. Retrieval resolves exactly one id and passes
        ``fetch_libraries`` for that id's tenant alone.

        The list and retrieval share ``allows``, so a picker built on this can
        never offer something retrieval would 404.
        """
        allows = getattr(identity, "allows", None)
        # One upstream listing per tenant, however many ids it yields. Resolving
        # each derived id separately would turn N libraries into N calls — the
        # eager-enumeration cost #925 removed from retrieval, reintroduced on the
        # one route that legitimately enumerates.
        fetched: dict[str, tuple[str, ...]] = {}
        for tenant_id in self.auto_tenants:
            libraries = self.auto_libraries.get(tenant_id)
            if libraries is None and fetch_libraries is not None:
                libraries = fetch_libraries(tenant_id)
            fetched[tenant_id] = tuple(libraries or ())
        resolver = replace(self, auto_libraries=fetched)

        ids = set(self.manual)
        for tenant_id, libraries in fetched.items():
            ids.add(auto_knowledge_id(tenant_id))
            ids.update(auto_knowledge_id(tenant_id, library) for library in libraries)

        out: list[DifyKnowledgeBinding] = []
        for knowledge_id in sorted(ids):
            binding = resolver.resolve(knowledge_id)
            if binding is None:
                continue
            if callable(allows) and not allows(knowledge_id, binding.tenant_id):
                continue
            out.append(binding)
        return out
