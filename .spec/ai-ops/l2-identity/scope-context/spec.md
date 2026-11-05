---
title: scope-context
desc: 把授权输入收敛成不可变 ScopeContext，且解析器不持有任何数据源
hue: 120
code:
  - src/aiops_diagnostics/identity/scope_context.py#ScopeContext
---
# scope-context

一次诊断运行的授权输入收敛为一个不可变值。它是下游所有存储与授权的键。

## raw source

「解析器不持有任何数据源」是这一层的形状约束，不是实现细节：因为不持有，
所以解析阶段**不可能**顺手查一次业务库；fail closed 因此是结构事实而不是纪律。

## 不变量

- 调用者身份只来自有效平台凭证，**绝不由请求体 `user_id` 推导**。
- 目标主体与调用者**显式区分**：B 端 `SysUser.id`、绑定的 C 端 `SysUser.userId`、
  `tenant_id`、`organ_id`、`shop_id/site_id` 与业务数据范围互不顶替。
- 主体不存在 / 映射歧义 / 租户越权 / 范围为空 / UPMS 或 Dis 不可用 ⇒ **立即 fail closed**，
  不回退为无范围直查，也不触发任何业务数据库查询。
- 凭证只透传给目录服务，绝不进入 `ScopeContext`、审计摘要或错误消息。
- `permissions` 记**本链断言过的能力集**：`ASSERTED` 链记凭据声明的原集合，
  `DERIVED`/`UNASSERTED` 链记空集——它们没有断言任何能力，写非空就是把
  「路由问它要什么」冒充成「凭据说它有什么」（#1040）。
- `capability_source` 记能力档位本身，档位规则只有一份（`capability.py`）；
  未知档构造即拒。`permissions` 与 `capability_source` 都**不进**
  `scope_fingerprint`——能力档位不改变任何可见性判定。
- `scope_fingerprint` 变化即下游缓存与授权失效。
