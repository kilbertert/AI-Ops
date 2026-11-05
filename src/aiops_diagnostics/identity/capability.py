"""「调用者能做什么」的单一权威（#1040）。

「一个调用者能不能做这件事」是能力（capability）规则。它曾经有四种渲染，
其中两条生产身份链把路由请求的 ``required_scope`` **原样写回**
``context.permissions`` —— 于是判据不是「你有没有这个能力」，而是「我问你
的时候你是不是正好说你有」：连凭空拼出的 scope 也被「授予」。同一个字段
在四条链上有两种语义——断言与回声。

现在给每条链一个**显式档位**，写进 ``ScopeContext.capability_source`` 并随
``scope_json`` 持久化。审计从此只说事实：``permissions`` 是「本链断言过的
能力集」，不再可能是「路由问它要什么」。

* ``CAPABILITY_ASSERTED`` —— 凭据本身断言了 scope 集合
  （``IntrospectionCallerResolver``；以及合成上下文里「代码自己写下这组
  能力」的位置）。``capability_verdict`` 只在这一档真正做成员判定：
  缺 ``required_scope`` 即 ``REFUSE``。
* ``CAPABILITY_DERIVED`` —— 能力由**目录**推导（公司权限码 → 角色，
  ADR-0084），请求的 scope 不参与授权判定。会话链与公司令牌链属此档：
  它们的 ``permissions`` 记空集。
* ``CAPABILITY_UNASSERTED`` —— 本链无法判定能力，只判身份与租户；需要
  能力的管理面由角色门另行拒绝（``UpmsCallerResolver``：公司 cloud-auth
  暴露的是平台权限而非资源 scope，拿它当能力判据本来就是错配——这正是
  被删除的「有任意权限就放行」检查错在的地方）。

判据只有一张表：``capability_verdict``。对非 ASSERTED 档答案是恒定的
``NOT_ASSERTED`` —— 链上不存在「能力够不够」这一问，而不是「答了但答错」。
落库的 ``permissions`` 集合遵守一条同样的单行规则：``ASSERTED`` 记凭据断言
的原集合，其余两档记空集 —— 由语法级守卫钉住（``src/`` 内
``permissions=frozenset({required_scope})`` 的形状即红）。
"""

from __future__ import annotations

CAPABILITY_ASSERTED = "asserted"
CAPABILITY_DERIVED = "derived"
CAPABILITY_UNASSERTED = "unasserted"
CAPABILITY_SOURCES = frozenset({CAPABILITY_ASSERTED, CAPABILITY_DERIVED, CAPABILITY_UNASSERTED})

VERDICT_GRANT = "grant"
VERDICT_REFUSE = "refuse"
VERDICT_NOT_ASSERTED = "not_asserted"


def capability_verdict(source: str, required_scope: str, asserted_scopes: frozenset[str]) -> str:
    """这条链对「请求的 scope」给出的答案，三条链同读一张表。

    - ``ASSERTED``：``required_scope`` 在凭据断言的集合里 ⇒ ``GRANT``，否则
      ``REFUSE``（唯一会产生 403 的档）。
    - ``DERIVED`` / ``UNASSERTED``：恒定 ``NOT_ASSERTED`` —— 这两档不回答
      能力问题，授权由角色与数据范围承担。
    """
    if source == CAPABILITY_ASSERTED:
        return VERDICT_GRANT if required_scope in asserted_scopes else VERDICT_REFUSE
    if source in (CAPABILITY_DERIVED, CAPABILITY_UNASSERTED):
        return VERDICT_NOT_ASSERTED
    raise ValueError(f"unknown capability source: {source!r}")
