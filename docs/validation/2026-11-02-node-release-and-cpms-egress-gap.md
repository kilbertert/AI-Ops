# 节点发版历史与 CPMS 路径误判纠正（2026-11-02）

> 后续复核已否定“管家直连公网 CPMS”的归因。本文保留发版与回退历史，
> 后续修复由 #1302 承接；不能据本文增加公网出口或把管家授权送往消费者代理。

历史环境：41 生产。网关 `1.0.0+6a2c6e43b9cd`，节点 release `2a59a18e` / jar `6e9a8a43…`。

## 一、部署了什么

| PR | 内容 | 交付方式 |
| --- | --- | --- |
| #1296 | 会话回合按 `applies_to` 声明补齐工具（ADR-0191） | CD 自动部署 |
| #1299 | 节点 schema 改从声明生成（读 `model_inputs`） | **手工发版**（不在 CD 路径里） |

节点发版按 `deploy/node-runtime/README.md` 的「仅更新 Java 节点 jar」：

```
git archive 2a59a18e services/execution-node → 干净源码 → mvn package
jar sha256 = 6e9a8a43…（与 images.lock.json 逐字一致，构建可复现）
cp -a 旧 release → 新 release，只替换 node/aiops-execution-node.jar
原子替换 runtime.env 的 AIOPS_NODE_DIST / AIOPS_NODE_BUILD
docker compose up -d --no-deps --pull never --force-recreate node
启动 61–120 秒；其他容器 ID 全部未变
```

## 二、结果：两个缺陷修好一个，暴露一个

### 2.1 schema 漂移 —— **已修复**

发版前：`ExecConfigViolation: 有效工具定义与清单不一致，期望 51432c97… 实际 ff1b2d88…`，
节点拒绝启动会话，**连订单诊断都不可用**。

发版后：节点认领作业（`actual_node_id=node-41`）并正常返回。错误消失。

**验证**：本人订单 → `completed` / `succeeded`。

### 2.2 站点仍失败，公网探针不能证明实际访问路径

当时站点查询返回 `cpms_unavailable`。节点到公网 CPMS 不通、内部回调可达，
但这是两个网络探针的结果，不能推出该次工具究竟访问哪个地址。

代码与后续真实 Java/Gateway 复核证明：管家授权的 `base_url` 已指向
`/v1/internal/operator-runs/{run}/cpms`，消费者另走 `/v1/internal/consumer-tools/{run}`。
两者凭据、主体和范围校验不同，不能互换。

后续 #1302 复现了代理请求匹配、站点校验、分页身份字段、授权请求体和二次授权丢证据问题。
原先“直连缺口”的结论撤销；不再据此申请公网出口或设计重复代理。

## 三、发版过程中的一个操作失误

看到站点仍失败就回滚节点到旧 jar。**这是错的**：#1296 与 #1299 是**不可拆的一对**——
网关已下发 `cpms_site_search`，旧节点不认识它的 schema，回滚后连订单诊断都挂了
（01:09–01:11 连续 4 次 `OPERATOR_DIAGNOSIS_FAILED`，`ExecConfigViolation`）。

已修正：把新 jar 装回。教训见下。

## 四、四条教训（本轮反复踩到的同一族错误）

| # | 错误 | 应做什么 |
| --- | --- | --- |
| 1 | 拿已过期的设计文档当现状（`tool-architecture.md` §6 的「未实现，需 P1–P3」） | 文档写的是**意图与计划**，断言现状要读代码 |
| 2 | 断言「模板缺项 ⇒ `tools=[]`」 | `candidate()` 在那种情况下**抛错拒绝发布**，不会产出空表——读守门条件就能排除 |
| 3 | 验收件读声明侧字符串而非**实际构造出的工具** | 判据必须读被测路径真正用的那份值，否则回退修复后仍全 PASS |
| 4 | 本地摘要 `81b96fd2…` vs CI `cfc66c4d…` | 本地构建产物（`m3node/build/*.class`）被算进上下文摘要；**两边都要在干净树上算** |

**共同点：用文档、夹具或自己刚写的值当事实，而不是读运行时真实值。**

另加一条流程教训：**两个 PR 构成一对时，回滚其中一半会让系统比不改更糟。**
回滚前必须先回答「这两处变更能否独立回退」。

## 五、当时状态（不是当前部署）

| 项 | 状态 |
| --- | --- |
| 网关 | `1.0.0+6a2c6e43b9cd`，CD 正常 |
| 节点 | `2a59a18e` / `6e9a8a43…`，运行中 |
| 订单诊断 | **正常**（本人订单 `completed`/`succeeded`） |
| 站点查询 | **不可用**（`cpms_unavailable`） |
| A–L | 通过 15 / 部分 3 / 未验证 0 |

回滚材料保留：`/var/backups/aiops-41/node-pre-1299/runtime.env`，旧 release
`/opt/aiops-node-runtime/releases/5ab4e4cf6a89`（**注意：单独回滚不可行，理由见 §三**）。

## 六、下一步

按 #1302 的真实代理整链证据修复，并将 Gateway、节点和回调配置配套交付。
部署后用原 H5 的站点与订单问题复验，更新当前交付索引。历史发版成功不代表站点功能已验收。

**未完成业务验收**：D/H/I 的遗留以最新证据为准。
