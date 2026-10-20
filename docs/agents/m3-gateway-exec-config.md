# M3-GATEWAY 执行配置身份：契约与复跑

本文件是 Gateway 侧执行配置守卫的**可执行契约**。它不是验收证据 —— 证据在
`docs/validation.md`，复跑命令在 `qa-plan.md` 的 `EXECCFG-GATEWAY` 一节。
节点侧守卫是另一件事，见 `tools/m2_close/m3node/README.md`。

## 0. 边界

- 不做通用版本平台、不做多版本并存或回滚编排。
- 不把节点自报的摘要当制品证明：**部署侧核验**与**节点受信声明**是两件事，分开记录。
- 不读生产凭据、不访问 41、不调真实供应商；验收全部在一次性数据根 + 合成/回环上游上完成。
- **节点发送端尚未接线**：本契约约束的是 Gateway 判据，不声称真实节点已经在回传身份。

## 1. 身份从哪来

| 项 | 来源 |
|---|---|
| 期望执行配置身份 | 受控清单文件的**原始字节** sha256（与节点侧同算法，不做 JSON 归一化） |
| 清单文件路径 | `AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE`（空＝未启用；非空但不可读/不合格 ⇒ **启动即拒**） |
| 节点实际身份 | 领取请求头 `X-AI-Ops-Exec-Config`（**只作对照输入**，不单独作为证明） |
| 记录里的身份 | `agent_runs.expected_agent_version`（签发）/ `observed_agent_version`（领取）/ `exec_config_state` |

**为什么是原始字节**：对解析后的对象重新序列化会引入 JSON 归一化差异（空白、键序、
数字表示），让两端对同一份语义算出不同摘要。原始字节没有这个自由度。

## 2. 清单格式（两端同一组必填字段）

```json
{
  "schema": "m3-exec-config/1",
  "name": "<非空字符串>",
  "history_policy": "none",
  "model": {
    "provider_key": "<非空>", "adapter_key": "<非空>",
    "model_key": "<非空>", "endpoint": "<非空>"
  },
  "model_options": { "temperature": 0.2, "top_p": 0.9, "max_tokens": 64 },
  "system_messages": ["<非空字符串，至少一条>"],
  "tools": [{ "name": "<非空>", "description": "<非空>", "input_schema": "<非空>" }]
}
```

拒绝条件（两侧逐条一致）：文件不可读、非普通文件、符号链接、组/他人可访问、空文件、
非 JSON、`schema` 不受支持、`history_policy` 不是 `none`、`model` 缺失、
`model_options` 缺失、`system_messages` 空、`tools` 空、任一模型键或工具三项缺失/空白。

**两条一致性由 `tools/m2_close/m3node/manifest_parity.py` 钉住**：同一组样本分别喂给
Python 与 Java 的真实加载器，逐条比对结论，接受样例还要比对身份逐字相同。
`model_options` 与工具的 description/schema 曾经是「Gateway 收、节点拒」的缝。

## 3. 三个时机

| 时机 | 判定 | 失败表现 |
|---|---|---|
| 启动 | 清单存在且合格；配了清单却没配委派绑定表 | **启动失败**（`ValueError`），不降级 |
| 签发 | 每次**重读**清单并写 `expected_agent_version` | 读不到 ⇒ 503 `EXEC_CONFIG_UNAVAILABLE` |
| 领取 | 回传头与期望逐字相等，**且此刻未过期** | 不等/缺失 ⇒ 记录落终态、403 `AGENT_RUN_EXEC_CONFIG_DRIFT`、**不返回快照**；已过期 ⇒ 既有 `expired` 语义、403 |

**每次签发重读**：新 run 使用签发当时的清单身份；已签发 run 继续按记录中固定的期望
核对，直到过期或撤销。更换清单不会回填旧记录，也不等同于撤销已有 run。

## 4. 状态与终态

`agent_runs.state` 仍是既有的单向路径；`exec_config_state` 只描述身份核对：

| 取值 | 含义 |
|---|---|
| `NULL` | 未启用清单（或迁移前的历史行） |
| `expected` | 签发时固定了期望，**尚未核对** |
| `matched` | 已核对且一致 |
| `mismatched` | 已核对但不一致 |

**「已固定期望」不是「已核对成功」**。回执里 `agent_version` 只在 `matched` 之后给出；
否则 `null` + `agent_version_gap="not_confirmed"`；未启用时 `gap="not_wired"`，
且回执**字段集合与升级前逐字相同**（不出现 `agent_version_observed`）。

**漂移落终态，且不复活**：`issued`/`running`/`succeeded` 三种起点都钉成
`failed` + `reason="exec_config_drift"`；已是 `failed`/`revoked`/`expired` 的记录
不被本路径改写、不复活、原因不被覆盖。

**审计**：漂移时把节点**实际**回传的摘要写进 `observed_agent_version`、状态写
`mismatched`，原因落在既有的 `reason`。不新增原因列、不扩展统计设施 —— 第二个真相来源
比缺一张统计表更糟。

**有效期在放行事务内核对**：`expires_at <= now` 即过期（与 `claim`/`complete` 的
`expires_at > now` 互补，也与会话外读取的 TTL 收敛同一条语句）。拿记录的那一刻有效
不等于放行这一瞬间有效 —— 只查 `state` 不查 `expires_at`，过期后三条放行路径都还能
交出业务正文。过期沿用既有语义：收敛为 `expired` + `ttl_elapsed`，不放行、不写 `matched`。

## 5. 并发与放行

身份比较、有效期判定、记录写入、以及**据以放行的状态**必须来自同一次事务读到的对象。
实现上是 `AgentRunStore.confirm_exec_config()`：一个写事务里先按既有语义收敛过期，
再读、比、写、再读。写不进去就是不放行，返回给调用方的是**写入之后**读到的记录。

放行以最终写事务提交为判定时点：此前提交的撤销必须被看见；此后提交的撤销阻止后续领取，
不能撤回已经获准发送的响应。网络发送不与 SQLite 事务原子提交。

三条放行路径共用同一段判定：首次领取、成功快照的重试、领取竞争失败后读到的那条结果。
R1-1 的教训是放行路径各有一次机会，只覆盖其中一条会让另一条漏掉。

## 6. 部署与回滚

- **默认关闭 ≠ 生产库无变化**：无论清单是否启用，`AgentRunStore` 初始化都会用
  `PRAGMA table_info` + `ALTER TABLE` 幂等补三列。合并即触发该迁移。
- 旧行三列保持 `NULL`，**不回填、不猜**；历史列逐字不变。
- **回滚**：清空 `AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE` ⇒ 走未启用分支；代码回滚后三列
  成了旧代码可忽略的附加列。已用**升级前的 `agent_runs.py`** 对升级后的库实测可读。
- 证据：`tools/m2_close/m3node/verify_migration_rollback.py`（9 条判据）。

## 7. 验收（见 `qa-plan.md` 的 EXECCFG-GW-01..17）

单测不够。对外行为变化必须用**真实进程**驱动：

```bash
uv run python tools/m2_close/m3node/verify_gateway_exec_config.py --out /tmp/exec-config-http.json
```

它在一个一次性数据根上起真 `create_gateway_app`，用回环 CPMS 与平台替身，
覆盖一致 / 缺失 / 错摘要 / 兄弟节点四种组合，并直接读数据库核对终态。
`/health` 200 不能代替这些判据。

---

## 8. 整轮终局：把「工具领取成功」与「整轮完成」分开（M3-NODE-LINK-01）

上一节的三层判定只回答「这次领取是不是跑在我方受控配置上」。它**不**回答
「这一轮模型工具循环走完了没有」—— 领取成功只说明业务数据已经取回。

### 8.1 状态：复用主状态，不新增列

| `state` | 快照 | 启用清单的新 run | 未启用 / 旧记录 |
|---|---|---|---|
| `running` | 无 | 已领取、正在取数 | 已领取（瞬间即逝） |
| `running` | **有** | 已取数、交回节点，**等待整轮确认** | 不出现 |
| `succeeded` | 有 | **整轮已确认** | 领取时直接落（既有语义） |

`complete()`（保存快照）按 `expected_agent_version` 是否为空分流：为空 ⇒ `succeeded`；
非空 ⇒ 写快照但**保持 `running`**、`completed_at` 留空。转 `succeeded` 只能由
`complete_loop()` 完成，它的条件全部在同一个写事务里：

- 记录有期望身份，且**本次回传的身份与之逐字相等**（领取时的 `matched` **不**授权
  另一份配置提交完成 —— 每个通知都要自己证明自己）；
- 仍是 `running` **且已有快照**（没取数就不存在「整轮完成」）；
- 此刻未过期（`expires_at > now`，与 `claim`/`complete` 互补）。

取消与进程退出没有回调 ⇒ 记录停在 `running`，由既有 TTL 收敛为 `expired`。
**`running` + 快照永远不会被读成成功。**

### 8.2 完成通知端点

`POST /v1/internal/charging-status/completions/{run_id}`
体：`{"status":"completed"}` 或 `{"status":"failed","reason":"<短码>"}`（严格模型，多一个字段 422）。

- 凭证与领取同一份节点令牌；绑定核对（节点 / 环境 / app / agent）与领取同一段代码，
  在任何写之前完成。响应**不含业务快照**：`{"run_id","state"}`。
- 幂等：同状态重复通知 200、不改 `completed_at`/`reason`；已完成后再报失败 409；
  终态（`revoked`/`expired`/`failed`）403；未启用清单 409 `AGENT_RUN_COMPLETION_NOT_TRACKED`；
  `issued` 或无快照 409 `AGENT_RUN_COMPLETION_CONFLICT`。
- `failed` 可以从 `issued` **或** `running`（含已取数）落终态 —— 首次领取就失败的那条路也算。

### 8.3 节点侧：整轮结束的判定必须 fail closed

节点接管 `ChatSessionRuntime`（自动配置里它是普通 `@Bean`，所以用 `@Primary` 子类；
实测 gRPC handler 持有的确实是子类实例），把 `completionConsumer` / `errorConsumer` 包一层：

1. 一个 order 晚于 `ToolCallingAdvisor` 的 `BaseAdvisor` 逐轮记录 **finish reason**；
2. 完成回调到达时，**最后一次 finish reason 必须是 `STOP`**，否则按失败处理；
3. 然后向 Gateway 发完成通知，**只有 2xx 才算确认**；未确认 ⇒ 走 `errorConsumer`。

为什么必须有第 2 步：实测末轮被截断（SSE 无 `[DONE]`、无 `finish_reason`）时，
框架照样触发完成回调、completion 文本是半截，finish reason 是 `_UNKNOWN`。
「有完成回调」不能证明上游把最后一轮发完了。

为什么受控领取失败要抛异常：`ToolExecutionException` 会被框架默认的
`DefaultToolExecutionExceptionProcessor(alwaysThrow=false)` 转成文本继续跑，
于是 `ERROR: …` 会让整轮"成功"结束。启用清单时工具抛 `RuntimeException` 中止整轮；
未启用时保留 M2 的文本行为。

### 8.4 部署顺序与回滚

1. **Gateway 先**：本片不新增列（复用主状态），只有新端点与新判定。
2. **节点后**：启用清单后才发身份头与完成通知。
3. **回滚**：清空两侧清单 ⇒ 回到未启用分支；旧节点不发通知，新节点在未启用时也原样透传。

### 8.5 验收

```bash
M3NODE_DEPS=… M3NODE_JDK_IMAGE=… uv run python tools/m2_close/m3node/verify_node_link.py --out /tmp/node-link.json
```

真 Gateway 进程 + 真 Java 节点 + 合成模型/CPMS，26 条判据（LINK-01..10）。
平台派发入口是替身，但**领取与完成通知都由节点自己的代码发出**。
