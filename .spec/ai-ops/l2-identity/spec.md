---
title: l2-identity
desc: 身份与范围——把授权输入收敛成不可变的 ScopeContext，全部 fail closed
hue: 120
code:
  - src/aiops_diagnostics/scope_context.py
related:
  - src/aiops_diagnostics/query_scope.py
  - src/aiops_diagnostics/caller_auth.py
  - src/aiops_diagnostics/third_session_auth.py
  - src/aiops_diagnostics/company_token_auth.py
  - src/aiops_diagnostics/agent_runs.py
---
# l2-identity

回答「谁在问、他能看到什么」。这一层的输出是下游所有存储与授权的键。

## raw source

**调用者身份只能来自有效平台凭证，绝不由请求体里的 `user_id` 推导。** 这不是防御性编程，
是这一层的定义：一条从请求体取 `user_id` 当授权依据的代码路径，无论多小心，都是把授权
交给调用方。目标主体（被排查对象）与调用者分离，只有调用者具备既有代查/管理能力时才允许使用。

## 不变量

- **fail closed 是唯一失败方向。** 主体不存在、映射歧义、租户越权、范围为空、UPMS/Dis 不可用
  —— 一律拒绝，**不回退为无范围直查**，且不触发任何业务数据库查询（解析器不持有数据源）。
- 站点集合为 `None`（all）与 `()`（空）是**不同**的语义：空集合表示最终可见站点为空，
  查询短路返回空证据，不发起数据库请求。
- **冻结身份的重建必须校验指纹。** `agent_runs.py` 把 `ScopeContext` 序列化进授权记录，
  读回时要求已有合法的 `scope_fingerprint` 并核对既有范围语义；缺失、空值或不符均拒绝重建。
  指纹不是完整快照签名，不覆盖 permissions、resolved_at 等非指纹字段，也不防可重算摘要的数据库写入者。
  记录里的身份只用于**再授权一次**，不是授权本身。
- 凭证只透传给目录服务，绝不进入 `ScopeContext`、审计摘要或错误消息；
  `ScopeContext` 不持久化任何数据库凭据或完整权限副本。
- `scope_fingerprint` 是下游键：范围变了，键就变，缓存与授权随之失效。
