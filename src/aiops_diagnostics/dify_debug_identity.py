"""面向 Dify 的**调试身份**（#586 / PRD #577）。

## 这一层在解决什么

运营在 Dify 的调试预览里要能打到**真数据**，否则改完提示词无法判断效果。但那条路
不能借用生产凭据 —— 借了就等于把调试面和生产线焊死：一次预览失误就是一次生产事故。

所以身份由**凭据**决定，且只能由凭据决定：

| 凭据 | 身份 | 能到哪 |
|---|---|---|
| `AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY` | `production` | 登记表里全部已登记的 `knowledge_id` |
| `AIOPS_GATEWAY_DIFY_DEBUG_API_KEY` | `debug` | **只有**固定测试租户下、列在调试子集里的 id |

## 为什么"由凭据决定"是这一层的全部要点

请求能带的东西太多了：头、查询串、body 里的 `knowledge_id`、`retrieval_setting`。
只要其中任何一个能影响"我是谁"，这条边界就不是边界 —— 它只是一个默认值。
因此 `DifyIdentity` **不带任何来自请求的字段**，解析函数也不接受请求对象：
调用方想给调试身份提权，没有可传的参数。

`DifyIdentity` 的字段刻意只有两个，且都是**收窄**用的：

* `fixed_tenant`：调试身份的租户。生产身份为 `None`（租户来自登记表）。
  非 `None` 时，与它不符的登记行**一律当作未登记** —— 不是 403，是 404：
  403 会说"这条存在但你不能用"，那本身就是一次泄漏。
* `knowledge_ids`：可用的知识库子集。`None` 表示不额外收窄（生产身份的语义）。

## 只读

本层**没有**写能力可授予 —— Dify 可达的工具端点集合里目前只有知识检索这一条，
而它是只读的（见 `ops/dify-exposure-registry.md`，段 1 在 CI 里断言"不得出现写方法"）。
`DifyIdentity` 因此不携带"可写"这一维：给一个不存在的维度留开关，是给未来的自己
留一个会写错的字段。等真的有第二个只读工具时，这里的是"它能用哪几个"的列表。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 身份名。两个字符串进了日志与错误码，所以是常量而不是散落的字面量。
DIFY_IDENTITY_PRODUCTION = "production"
DIFY_IDENTITY_DEBUG = "debug"

DIFY_IDENTITIES = (DIFY_IDENTITY_PRODUCTION, DIFY_IDENTITY_DEBUG)


@dataclass(frozen=True, slots=True)
class DifyIdentity:
    """谁在调这条路由 —— 由凭据决定，不带任何来自请求的字段。"""

    name: str
    #: 调试身份固定的租户；生产身份为 None（租户由登记表决定）。
    fixed_tenant: str | None = None
    #: 可用的 knowledge_id 子集；None = 不额外收窄。
    knowledge_ids: tuple[str, ...] | None = None

    @property
    def is_debug(self) -> bool:
        return self.name == DIFY_IDENTITY_DEBUG

    def allows(self, knowledge_id: str, tenant_id: str) -> bool:
        """这条身份在**这个** `knowledge_id` 上可用吗。

        两个条件都只在调试身份上生效，且顺序无关：租户必须是固定的那个，
        id 必须在子集里。任一不符都返回 False —— 调用方把它当"未登记"处理（404），
        不区分"不存在"与"不该你看"。
        """
        if self.fixed_tenant is not None and tenant_id != self.fixed_tenant:
            return False
        return self.knowledge_ids is None or knowledge_id in self.knowledge_ids


#: 生产身份：与 #581 逐字一致的行为。
PRODUCTION_IDENTITY = DifyIdentity(name=DIFY_IDENTITY_PRODUCTION)


def debug_dify_identity(settings: object) -> DifyIdentity:
    """Build the debug identity from settings — the only place it is constructed.

    ``fixed_tenant`` comes from configuration and nowhere else; ``knowledge_ids``
    is the explicitly listed subset. An **empty** list stays an empty list (the
    identity can reach nothing, i.e. falls back to no ``knowledge_id`` matching
    ``allows``) rather than becoming ``None``: ``None`` means "no narrowing",
    and a missing subset must never quietly mean that.
    """
    raw = str(getattr(settings, "dify_debug_knowledge_ids", "") or "")
    ids = tuple(dict.fromkeys(item.strip() for item in raw.replace(";", ",").split(",") if item.strip()))
    return DifyIdentity(
        name=DIFY_IDENTITY_DEBUG,
        fixed_tenant=str(getattr(settings, "dify_debug_tenant", "") or "").strip(),
        knowledge_ids=ids,
    )
