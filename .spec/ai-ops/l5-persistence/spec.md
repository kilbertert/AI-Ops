---
title: l5-persistence
desc: 同一个 SQLite 文件，各 store 各自建表
hue: 30
code:
  - src/aiops_diagnostics/gateway/gateway_store.py
related:
  - src/aiops_diagnostics/common/async_job_lifecycle.py
  - src/aiops_diagnostics/conversation/conversation_store.py
  - src/aiops_diagnostics/gateway/metrics_store.py
  - src/aiops_diagnostics/publication/agent_lifecycle.py
  - src/aiops_diagnostics/publication/shortcut_lifecycle.py
  - src/aiops_diagnostics/dify/dify_app_registry.py
  - src/aiops_diagnostics/execution/agent_runs.py
---
# l5-persistence

Gateway 的存储面。同一个 SQLite 文件上挂了多个 store，各自建表。

## raw source

`GatewayStore` **没有 Protocol**：接缝是鸭子类型 + 构造注入。类文档写明「接口留待日后迁移到
PostgreSQL」——这是一条已声明的迁移债，不是遗漏。

## 不变量

- **异步作业生命周期只有一处定义**（`async_job_lifecycle`）：四张表按命名 profile 声明各自
  差异，而不是各写一份过期语句与 claim guard。副本的代价已经量过：同一规则曾有五份手写副本、
  两种极性拼写、两套常量族。
- **指标按构造脱敏**：只存租户/路由/计数/延迟/失败码，**从不**存问题正文、答案正文、
  提示词、知识库内容、媒体 URL、令牌或存储路径。长期监控数据的形状由写入处决定，不由读取处约定。
- **会话上下文不得代替当次鉴权**：每一轮都对着**当前请求**重新校验范围、入口、智能体与订单授权；
  存下来的上下文只是便利，不是凭证。
- **授权记录的状态迁移全靠条件 UPDATE。** 每次迁移都是一条 `WHERE ... AND state = ...`，
  以受影响行数判定谁赢；不引入队列或额外锁服务。重启不得把 `running` 改回 `issued`——
  那会让同一笔授权被读第二次。建表只用 `CREATE TABLE IF NOT EXISTS`，不覆盖同库其他表。
- Dify 没有租户概念，所以 `(租户, 业务入口) → app` 的判定只能存在于这里，显式地、作为数据存在。
  查表**只返回绑定或空，绝不推断**；表里一旦有任何一行，未登记组合**什么都不选**（fail closed）。
