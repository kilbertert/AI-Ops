# D 方案决策记录 — 充电业务诊断查询接口（AI-Ops 直查库正式化）

- 决策时间：2026-08-27
- 决策状态：已锁定（D 方案；TDengine 段暂留直连，等 tsdata 补洞后回收）
- 决策人：ranlei（经多轮 grill + 方案对比）
- 决策上下文：handoff-brief.md（2026-08-27 创建）+ docs/diag-query-api-plan.md（2026-08-25 合入）
- 记忆引用：团队记忆 mem-20260827-ranlei-005（候选期，待人工批准；本记录只做同步与追溯，不以候选记忆代替正式批准后的决策结论）

---

## 1. 四个候选方案对比

| 方案 | 核心动作 | 阻塞点 | 代价 |
|---|---|---|---|
| **A** | 在 `/iot/tsdata` 上游开 PR 补 token 校验 | 跨仓 review + 维护负担 | 高（运维成本长期） |
| **B** | 镜像 tsdata 到本机，自己维护 | 镜像同步 + 部署责任 | 高（承担运营） |
| **C** | 在 Java 侧网关加 token 校验/前置代理 | **需改 Java 仓库** | 中（违反“不动 Java”约束） |
| **D（锁定）** | 仅 AI-Ops 仓内：HybridSources 4 HTTP + 2 TDengine 直连；MySQL/Redis 收口；TDengine 暂留 | 不需要跨仓 | 低（纯仓内） |

## 2. 锁定 D 的原因

- **不可接受的约束**：用户明确要求“不对 Java 仓库做任何修改”——排除 C。
- **不可接受的代价**：A 跨仓 review / B 运维承担均不可长期接受——排除 A/B。
- **D 的核心价值**：MySQL + Redis 凭据可在不碰任何外部仓库的前提下收回，TDengine 段标注“等 tsdata 真正补洞后回收”。

## 3. 影响的 issue 列表

- 父 PRD：#23
- 本轮 PR 序列：#50（PR-A）、#51（PR-B）、#52（PR-C）
- 外部阻塞项：#35（tsdata token 校验）已关闭；Phase 3b 前置条件等 tsdata 真正补洞后以新 issue 重启
- 暂不推进的 TDengine 段查询：#37、#38（等 tsdata 补洞后追加）

| Issue | 标题 | 原状态 | D 方案下状态 |
|---|---|---|---|
| #34 | T1: /diag/order 充电订单查询接口（Java） | DONE | 保持 DONE |
| #35 | T7: tsdata /tdadmin/* 令牌校验补洞 | BLOCKED | **关闭**（tsdata 补洞不在 AI-Ops 可执行范围；Phase 3b 以新 issue 重启） |
| #36 | T9: AI-Ops HttpSources 实现 + fixture 等价测试 | DONE | 保持 DONE |
| #37 | T2: /diag/comm-message 原始报文查询接口（Java） | OPEN（待 T1） | **关闭**（D 方案下不在范围内） |
| #38 | T3: /diag/gun-property 充电过程时序查询接口（Java） | OPEN（待 T1） | **关闭**（D 方案下不在范围内） |
| #39 | T4: /diag/occupy-order 占位费订单查询接口（Java） | OPEN（待 T1） | 保留（可由 Java 服务先行实现，后续可走 HTTP） |
| #40 | T5: /diag/redis-stream 同步队列查询接口(Java) | OPEN(待 T1) | 保留 |
| #41 | T6: /diag/device 设备信息查询接口(Java) | OPEN(待 T1) | 保留 |
| #42 | T8（改）: AI-Ops 内部令牌密钥从配置读取 + 验证窗口 300s | OPEN（待 T1 + T7） | 改为 **PR-A 范围**（D 方案下 AI-Ops 仓内先落密钥） |
| #43 | T10（改）: 收口 —— 删 MySQL+Redis 凭据（Phase 3a）；TDengine 留口子（Phase 3b 待 tsdata 补洞） | OPEN（待全部） | 改为 **PR-D 范围**（MySQL+Redis 凭据回收；TDengine 留口子） |

## 4. PR 任务清单

- **#50** PR-A → `docs/afk-cutover/pr-a-spec.md`（HybridSources 部分切流量 + fixture 等价）
- **#51** PR-B → `docs/afk-cutover/pr-b-spec.md`（doctor 分类 + `.env.example` 注释化）
- **#52** PR-C → `docs/afk-cutover/pr-c-spec.md`（方案文档与 AFK 工作流同步）
- PR-D：MySQL+Redis 凭据回收、TDengine 留口子；待后续以新 issue 落地，本文不落 PR 清单

## 5. 关键不变量（D 方案前后都必须成立）

- AI-Ops 直连 MySQL 凭据：从 `production.env` **消失**（PR-D 后）
- AI-Ops 直连 Redis 凭据：从 `production.env` **消失**（PR-D 后）
- AI-Ops 直连 TDengine 凭据：**保留**，直到 tsdata 真正补洞
- AI-Ops 直连 SSH 隧道：**保留**，直到 tsdata 真正补洞
- tsdata 仓库：**不动**
- 充电桩 Java 仓库：**不动**

## 6. 后续（Phase 3b）触发条件

当以下**全部**满足时，Phase 3b 可启动：
- tsdata 仓库已合并内部 token 校验(本仓不参与)
- 充电桩 Java 服务的 `/diag/comm-message` 与 `/diag/gun-property` 已实装
- D 方案触发时以新 issue 形式重启，不沿用已关闭的 #35
