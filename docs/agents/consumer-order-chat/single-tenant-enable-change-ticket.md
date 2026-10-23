# 变更单：个人订单状态查询 · 单租户开放（本片不启用）

日期：2026-10-23
状态：**变更单，未执行**。生产三项配置仍为空，`CONSUMER_ORDER_CHAT_ENABLED` 关闭。
依据：#976（实现）、#992（H5 转发）、#997（真实页面验收）、#1015（真实跨用户拒绝）。
本片只做只读核查与文档，**没有**开窗、没有提交业务问题、没有调用模型。

---

## 一、结论：建议执行（单租户、映射形式、限时观察）

| 项 | 结论 |
|---|---|
| 业务验收 | 两条真实页面正例 + 真实跨用户拒绝 + 停用回读拒绝，均已独立验收 |
| 影响面 | 近 7 天有**三个租户**产生问答/指标；目标租户 97/126 条问题、225/290 条指标。**三类信号源的口径不一致**，见 5.2 |
| 可逆性 | 只改三项进程环境变量 + 一次重启；恢复即逐字回到当前 sha256 |
| 主要代价 | 命中问法不再走模型（这是目的），未登记租户的同类问法变成 404（这是副作用） |
| 建议 | **执行**，但只登记目标租户，并接受第九节的观察局限 |

不建议"直接常驻开放全租户"：没有账号级灰度，开关是进程级的。

---

## 二、目标环境、租户、制品

| 项 | 值 |
|---|---|
| 环境 | 41（`api.mall.qushiyun.com`），逻辑执行环境 `41` |
| 目标租户 | `1942105476598861824`（下游哈希 `a424f36e3c0a5da1`，见第九节） |
| 合并制品 | `0.1.0+26bc23a34fd4`，CD run [`39910138218`](https://github.com/kilbertert/AI-Ops/actions/runs/39910138218)（`headSha=26bc23a34fd4f96ca9b7843b5e8d65eaae5bbb36`） |
| 制品是否含已验收实现 | 是：`a49be60`（#997 查询实现）是 `26bc23a` 的祖先 |
| 现状自查 | `curl http://172.18.0.1:8788/health` ⇒ 同上版本；`gateway.env` sha256 `0fb25006…` |

**上线前必须重取一次**：本片之后 `main` 若再有 `src/**` 合并，CD 会自动部署并重启，
制品身份要按那时的 `/health` 重新记录，不能用本文的值。

---

## 三、三项配置的变更范围

只改 `gateway.env`（systemd 的 `EnvironmentFile`），**不整文件覆盖**。

```dotenv
AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=true
AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=<target-tenant>:<url>
AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=41
```

**端点只引用受控位置，不在文档里写明文**：

- 形如 `tenant:url`，`tenant` 段就是第二节的租户 id；
- `url` 取 `/etc/aiops-41/gateway.env` 里 `AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL`
  中**同一个 tenant 前缀**的那一条（它是同环境已注册端点）。取法：

```bash
ssh aiops-41 'grep -E "^AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL=" /etc/aiops-41/gateway.env'
# 在输出里挑出以 <target-tenant>: 开头的那一段，整段作为新值
```

**为什么必须是映射形式**：旧标量形式（单条 `https://…`）对**所有租户**都解析成功，
等于对全平台开放；映射形式只对登记过的租户解析，其余租户 `TENANT_ENDPOINT_UNRESOLVED`。

**写入方式**（产物身份，任一前置不符即拒绝，不会覆盖别人的并发改动）：

```bash
dev-host write 41 /etc/aiops-41/gateway.env \
  --if-sha256 <当前本机读到的 sha256> \
  --artifact-sha256 <新文件 sha256> < candidate.env
```

`AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL`、M3 两项、其余键**一个都不能动**。

### 改之前先验一遍候选配置

只构造 settings 跑 `validate()`，**不要构造 app**（构造 app 会调用
`recover_interrupted_jobs`，把在飞作业标成 failed）：

```python
# 在 41 上、aiops41 账号
import os

from dotenv import dotenv_values

for k, v in dotenv_values("/tmp/candidate.env", interpolate=False).items():
    if k and v is not None:
        os.environ[k] = v

from aiops_diagnostics.gateway_config import GatewayServerSettings, charging_endpoint

s = GatewayServerSettings.from_env()
s.validate()
assert s.consumer_order_chat_enabled and s.execution_environment == "41"
assert charging_endpoint(s.charging_status_base_url, "<target-tenant>", require_registration=True)
```

三项缺一即启动失败（`consumer order chat requires its charging status endpoint`）——
配一半不会开门，这是设计。

---

## 四、其他租户会发生什么

| 场景 | 现在（关闭） | 开放后 |
|---|---|---|
| 目标租户问"查我最新一单状态" | 普通问答（模型） | **确定性查询**，202 completed，零模型 |
| 目标租户问别的问题 | 普通问答 | **不变** |
| **未登记租户**问同类问法 | 普通问答（模型） | **404 `TENANT_ENDPOINT_UNRESOLVED`**（行为改变） |
| 未登记租户问别的问题 | 普通问答 | 不变 |
| operator 入口 | 原有路由 | **不变**（判据里 `decision.platform == "consumer"` 才进新路径） |
| 简体/繁体/英文之外的问法 | 原有路由 | **不变**（language 白名单） |
| 快捷动作 | 原有路由 | **不变**（`not payload.shortcut_code`） |

**另有一个容易漏掉的变化**：`GET /v1/orders/{order_no}/charging-status` 现在因为
`charging_status_base_url` 为空而恒 404 `CHARGING_STATUS_DISABLED`；一旦配置了个人地址，
这条**直读入口对目标租户就变成可用**。它复用同一套 `consumer + self + 非代查 + 可见订单`
判据，不是更宽松的入口，但它确实是**新开的一个面**，观察期要一起看。

实测这条路由的历史流量：整个访问日志里共 **1 次**（10-18，404），10-21 那 23 次
`/v1/orders/*/access` 全部来自 `47.97.160.153`（本机自测）。所以它是一个**当前无人调用**
的入口——不是"没有风险"，而是"没有在用"，开放后要按新增面观察。

### 措辞匹配到底命中什么

`is_order_status_question` 要求**同时**满足：出现"订单/order"一类词、出现状态类词
（`状态`/`status`/`充电中`/`finished` 等）或"最新+查询"组合，且**不含**诊断类词
（`异常`/`故障`/`扣费`/`退款`/`为什么`/`diagnos\w*` …）。未命中的措辞落回原路由。
所以"开放 = 零模型"**不成立**，只能说"命中的那部分零模型"。

---

## 五、实际影响范围（本片只读核查结果）

数据来源：`gateway.db`（`mode=ro`）、`gateway.env` 键名、nginx 生效配置、访问日志元数据。
**只取元数据，没有读任何用户问题正文或订单正文。**

### 5.1 哪些入口把 `/v1/` 送到这台网关

`nginx -T` 里 `proxy_pass … 8788` 的宿主文件（共 5 个）：

| 文件 | 入口 | 注入的 `X-Business-Entry` |
|---|---|---|
| `rewrite/api.mall.qushiyun.com.conf` | `api.mall` 的 `^~ /v1/` | map：默认 `consumer`，`operator` 转发 |
| 同文件 `^~ /v1/dify/` | Dify External Knowledge | 不注入（Dify 不是我们的用户） |
| `extension/ulink.h5.../aiops-h5-assistant.conf` | H5 助手面（方法+路径白名单） | 硬编码 `consumer` |
| `vhost/aiops-41-8789.conf` | `api.qumall:8789`（allow 单 IP） | **透传客户端给的值** |
| `kb.mall.qushiyun.com.conf` | `/__aiops/knowledge/hop` → `/v1/internal/knowledge/hop` | 不涉及 |

`hst.h5` 的 `^~ /v1/dify/` 与 api.mall 的 Dify 那一跳同形。

> **注意 `:8789`**：它透传客户端头，不补 `X-Business-Entry`。经由它的请求若不带这个头，
> 开放后会拿到 `403 CONSUMER_SCOPE_REQUIRED`，而不是普通问答。该入口整份访问日志
> 共 **11 行**、跨 09-12 → 10-21，其中 `/v1/` 只有 **3 行**（最近一条 10-12，
> 且源地址是 `127.0.0.1`）。

### 5.2 哪些租户真的在用

`assistant_questions` 全量（不含正文）：

| 租户 | 问题数 | `run-`（模型） | `consumer-order-chat` | 首末 |
|---|---|---|---|---|
| 目标 `1942…824` | 112 | 107 | 5 | 10-13 → 10-23 |
| `1961…296` | 26 | 26 | 0 | 10-18 → 10-19 |
| `2084…000` | 3 | 3 | 0 | 10-21 |
| `(null)`（列未回填的历史行） | 359 | 359 | 0 | 09-12 → 10-12 |

网关日志近 7 天：`a424f36e…` 1388 条、`c9fd5005336ecf33`（=`1961…296`）2 条。
近 14 天里 10-14 之后每天只有目标租户。

> ⚠️ **三类信号源口径不一致，不能混着读**（本片实测）：
>
> | 信号源 | 近 7 天看到的租户 | 缺口 |
> |---|---|---|
> | 网关日志 `tenant_hash` | 2 个（目标 1388、`1961…296` 2 条） | 只记 `faq_platform_decision`，不走 FAQ 平台判定的请求不产生这一行 |
> | `assistant_questions` | 3 个（97 / 26 / 3） | 只有走问答面的请求 |
> | `agent_run_metrics` | 3 个（225 / 59 / 6） | 每次请求可产生多行 |
>
> 对同一时段的三个数：
>
> - `2084…000` 在 `assistant_questions` 里 3 条、`agent_run_metrics` 里 6 行，**网关日志里 0 条**；
> - `1961…296` 的问题只有 2 天（10-18/19），但指标还在；
> - **结论**：拿"日志里只有 2 个租户"去说"只有 2 个租户在用"是错的。
>   真实口径是"至少这 3 个"；日志覆盖不到的那部分，本片**未能归因**（可能是请求没走 FAQ
>   平台判定，如 `X-Business-Entry` 直接给出、或走了别的分支），记为**未观测**。

> **10-10 的一次异常**：当天日志出现 35 个租户、单租户 89 个不同主体。**本片没有定性**——
> 203.0.113.x 之外的内部地址、`/health` 高频、之后不再出现，看起来像一次性压测或评测。
> 它**不影响**开放结论（那时新路径还不存在），但"其他租户不会来"这句话不能拿它当反证。

### 5.3 历史上真正会命中新路径的问法

用实现里的 `is_order_status_question` 对**已存的 500 条问题**离线跑一遍（只计数，不输出正文）：

| 租户 | 命中（不含我这两次窗口产生的） | 时间 |
|---|---|---|
| 目标 | **5** | 10-22 ×2、10-23 ×3；`internal_run_id` 全部是 `run-` 前缀（即当时都走了模型路径） |
| `1961…296` | 0 | — |
| `2084…000` | 0 | — |
| `(null)` 历史 | 6 | 09-20、10-08、10-09 |

**这 6 周的留存数据里，其他租户没有一条命中**。但这是"未观测到"，**不是**"不会发生"——
样本只有 500 行、6 周，且 `(null)` 那批说明历史口径变过。

### 5.4 并发、超时、限流到底覆盖什么

| 机制 | 实际覆盖 | 对确定路径的意义 |
|---|---|---|
| nginx `limit_req` / `limit_conn` | **0 处使用**（`nginx.conf` 只定义了 zone，全配置无一条 `limit_*` 指令） | **没有任何限流** |
| Gateway 线程池 | 路由是**同步 `def`**，跑在 Starlette 线程池（anyio 默认上限 40） | 每个请求占一个线程 |
| 单请求外部调用 | 4 次 SQL（`list_orders` 1 次 + 可见性 3 次）+ 1 次 CPMS HTTP | 线程被占满这段时间 |
| SQL 上限 | `SET SESSION MAX_EXECUTION_TIME=3000` + 只读事务 | 单条 ≤ 3 s |
| CPMS 超时 | `read_charging_status` 硬编码 10 s | 单次 ≤ 10 s |
| 最坏单请求 | 4×3 s + 10 s ≈ 22 s（无重试） | nginx `proxy_read_timeout 120s`，不会被掐 |
| **实测单请求** | **139 ms / 148 ms**（#1015 两次真实页面查询，`created_at → completed_at`） | 最坏值是理论上限，与实测差两个数量级；观察期以实测分布为准 |
| QA 截止 | 15 min（但本路径同步完成，从不排队） | 观察期出现 `queued`/`running` = 落回了模型路径 |
| 共享 CPMS 端点 | 目标租户与 OPS 运维工具**用同一个地址**（sha256 前 12 同为 `51cf3382c693`） | 状态查询与运维工具共享该上游的容量 |

### 5.5 证据存在哪、怎么留

- 私有快照：`<AIOPS_GATEWAY_DATA_HOME>/consumer-order-chat/<qa_id>.json`，`0600`，
  含 `call_id`、`scope_fingerprint`、`snapshot`、`answer_sha256`、`result_sha256`；
- 现状 5 个文件，与 5 条 `consumer-order-chat:` QA 一一对应，**无孤儿**；
- **没有自动删除**：QA 行的保留期只影响"读不读得到"，不删证据文件；
- QA 保留：`QUESTION` 档 15 min 截止 / 15 min 保留，但 2026-10-23 起 `sweep`
  **不再把已完成的平台答案改写成 expired**，所以已交付的会一直是 `completed`。

---

## 六、执行步骤、观察、停止条件

### 6.1 开窗前（全部只读）

1. `curl /health` 记录版本；`sha256sum /etc/aiops-41/gateway.env /etc/aiops-41/production.env`；
2. 进程环境里三项**不出现**（`tr '\0' '\n' < /proc/<MainPID>/environ | grep -c …` 应为 0）；
3. `in_flight_qa` / `in_flight_dx` 均为 0；`agent_runs`、`conversation_turns` 计数记录；
4. 确认没有正在跑的 CD：`gh run list --workflow CD` 最近一条已 `completed`，
   且**没有排队中的 `src/**` 合并**（窗口期 CD 会重启网关）；
5. 备份 `gateway.env` 到库外私有目录（`0600`），算 sha256；
6. 按第三节做候选配置 + `validate()`。

### 6.2 开窗

```bash
dev-host write 41 /etc/aiops-41/gateway.env --if-sha256 <before> --artifact-sha256 <candidate> < candidate.env
ssh aiops-41 'systemctl restart aiops-gateway-41 && sleep 6 && systemctl is-active aiops-gateway-41'
```

**重启前提**：`in_flight_qa == 0` 且 `in_flight_dx == 0`。有在飞作业时先等它完成或按
41 手册先做处置——重启会把在飞作业标成 `failed`（`QA_INTERRUPTED_BY_RESTART`）。

**重启后立刻核对三项真的进了进程**（只写文件不重启＝没生效，这是 41 上踩过的坑）：

```bash
ssh aiops-41 'tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
  | grep -E "^AIOPS_GATEWAY_(CONSUMER_ORDER_CHAT_ENABLED|EXECUTION_ENVIRONMENT)="'
```

### 6.3 验证（窗口开着做）

| # | 动作 | 期望 |
|---|---|---|
| 1 | 目标租户在真实 H5 问"查我最新一单状态" | 202 `completed`；`internal_run_id=consumer-order-chat:<订单号>`；页面出订单/状态/来源/观察时间 |
| 2 | 同一会话正文指定**另一张**本人订单 | 返回那一张，不回落最新 |
| 3 | 另一名真实登录用户（同租户、不同主体）问同一张单 | 404 `ORDER_NOT_FOUND`，正文无订单号/状态/来源 |
| 4 | 该用户回读上一步的 QA | 404 `QA_NOT_FOUND` |
| 5 | 目标租户回读自己的 QA | 200 `no-store` |
| 6 | 用一个**未登记租户**的会话问同类问法 | 404 `TENANT_ENDPOINT_UNRESOLVED`（预期行为，见第四节） |

第 3、4 条必须有第 5 条做**对照**：同一入口、同一问句、同一时刻，只换调用者。
没有对照，"401/404 一律算拒绝"会把**头名写错**也读成通过（#1015 就出过一次）。

### 6.4 观察期与停止条件

**观察期**：以"至少 1 条**真实用户**（非本片测试账号）命中新路径"为达成条件，最长 **7 天**。
到期未命中 ⇒ 记录"未观测到真实使用"，**不延长，也不宣称已验证**。

**停止条件（任一触发即停用，按第七节恢复）**：

| 观察项 | 触发值 |
|---|---|
| `POST /v1/assistant/questions` 5xx 占比 | 窗口内 > 5%，或连续 3 次 |
| 未登记租户的 404 `TENANT_ENDPOINT_UNRESOLVED` | 出现即停（说明有别的租户在用） |
| 本应成功的请求出现 401/403 | 出现即停 |
| `consumer-order-chat:` 的 QA 出现 `queued`/`running`/`failed` | 任何一条即停 |
| CPMS 502/503 | 连续 3 次，或占比 > 5% |
| 单请求耗时 | p95 > 5 s |
| 同一 `qa_id` 同时有 `consumer-order-chat:` 标记与 `routing` 指标 | 出现即停（说明走了模型） |

---

## 七、恢复：只撤本次字段

```bash
# 1) 从备份里删掉本次新增的三行，其余保持文件当前内容（不整文件覆盖）
# 2) 按产物身份写回
dev-host write 41 /etc/aiops-41/gateway.env --if-sha256 <current> --artifact-sha256 <after> < restored.env
ssh aiops-41 'systemctl restart aiops-gateway-41 && sleep 6 && systemctl is-active aiops-gateway-41'
# 3) 核对恢复后的 sha256 与开窗前**逐字相同**
ssh aiops-41 'sha256sum /etc/aiops-41/gateway.env /etc/aiops-41/production.env'
```

**先停用、再看回读**。停用后：

- 新问："查我最新一单状态" → 回落到普通问答（`queued`，**可能调用模型**）；
- 回读：历史 `consumer-order-chat:` QA → `404 CHARGING_STATUS_DISABLED`；
- 直读 `GET /v1/orders/{no}/charging-status` → 回到 `404 CHARGING_STATUS_DISABLED`；
- 证据文件留在原处，不随停用删除。

⚠️ **回滚代码 ≠ 停用功能**（`docs/agents/consumer-order-chat/README.md` 已写）：
启用期间产生的 QA 带快照绑定，直接回旧代码会让旧会话逻辑重新使用这些快照。
要回滚代码，先停用。

---

## 八、不做的三件事

1. **不加限流、不加账号级灰度、不加租户注册表**——本片明确不做；
2. **不改实现**——第九节的最小修复建议留作下一轮决定；
3. **不处理 Git 历史脱敏**——独立治理事项。

---

## 九、观察口径：按请求关联，不用全局计数

### 9.1 能关联到什么

| 证据 | 键 | 粒度 |
|---|---|---|
| `assistant_questions` | `qa_id`、`internal_run_id`、`created_at`、`completed_at`、`tenant_id` | **每条作业一行** |
| `agent_run_metrics` | `route_type=qa`、`outcome`、`token_count`、`created_at`、`tenant_id` | **每条成功请求一行**，**无 `qa_id`** |
| 运行目录 `gateway/runs/<run_id>/` | `run-…-qa-xxxx` | 只有走模型/诊断才有 |
| **nginx 访问日志** | `time_local`、`remote_addr`、`request`、`status` | 每条 HTTP 一行，**无请求 id、无 upstream 耗时** |

`consumer-order-chat` 路径**整段没有日志**（`_consumer_order_chat` 里 `_LOGGER` 出现 0 次），
所以它既不在网关 journal 里，也不会自带请求 id。

**但 nginx 那层能按时间对上**（#1015 实测，H5 入口）：

```
15:25:10 GET /v1/shortcuts → 200, GET /v1/faq/recommendations → 200
15:25:10 POST /v1/assistant/questions → 202     ← QA created_at 15:25:10.643
15:25:11 GET  /v1/assistant/questions/qa_{id} → 200
15:25:25 POST ... → 202                          ← QA created_at 15:25:25.459
15:27:15 POST ... → 404; GET .../qa_{id} → 404  ← B 的两条负例
```

所以"按请求关联"能做到**同一秒 + 同一租户 + 同一路径**的交叉验证，**不是硬键**。
回执里必须这么写，不能包装成"逐请求追踪"。

> ⚠️ **我一开始查错了文件**：去 `api.mall` 的日志里找这次窗口的 POST，得到 0 条，
> 差点写成"访问日志里没有这次请求"。这次窗口走的是 **`ulink.h5`** 那份 vhost，
> 日志在 `/www/wwwlogs/ulink.h5.mall.qushiyun.com.log`。入口选错，观察就是空的。

### 9.2 逐条观察口径

```sql
-- ① 本次目标租户、明确命中状态查询的请求（**必须 LIKE，不能等值**）
SELECT qa_id, status, internal_run_id, created_at, completed_at, tenant_id
FROM assistant_questions
WHERE internal_run_id LIKE 'consumer-order-chat:%'
  AND created_at >= :window_start;

-- ② 这些请求的结果与耗时
SELECT qa_id,
       json_extract(result_json, '$.blocks') IS NOT NULL AS has_blocks,
       ROUND((julianday(completed_at) - julianday(created_at)) * 86400, 3) AS seconds
FROM assistant_questions
WHERE internal_run_id LIKE 'consumer-order-chat:%' AND created_at >= :window_start;

-- ③ 对应请求是否发生模型/诊断调用：三条一起看，缺一条都不能下结论
--    (a) token_count 必须为 0
SELECT count(*) FROM agent_run_metrics
WHERE route_type='qa' AND created_at >= :window_start AND token_count > 0;
--    (b) 同租户在 [created_at-1s, completed_at+120s] 内有没有 routing 行
--    (c) gateway/runs 下有没有该时间段新建的 run- 目录
```

**禁止**把"整台 Gateway 的 `runs` 新增为零"当通过条件：共享网关的其他请求
（FAQ、Promo、诊断、operator 工具）本来就会正常调用模型。

### 9.3 已知的观察盲区（必须写进回执，不能补写成"已观测"）

| 盲区 | 原因 | 后果 |
|---|---|---|
| **拒绝的错误码** | `_consumer_order_chat` 的失败分支**不写任何指标行**；`StandardAPIError` 处理器只回响应体 | 只能从 nginx 日志看到 404/502 的**状态**，看不到是 `ORDER_NOT_FOUND` 还是 `TENANT_ENDPOINT_UNRESOLVED` |
| **网关自身访问日志** | 近 7 天 journal 里 `"GET /v1/…"` 行数为 **0**（uvicorn access log 未启用） | 只能靠 nginx 那一层 |
| **上游耗时** | api.mall 用默认 combined 格式 | 看不到 `upstream_response_time` |
| **请求 id** | 全链路没有 | 跨层只能按秒对齐 |

### 9.4 最小修复建议（本片不实施）

让拒绝可归因，最小改动是**复用已有的 `_record_route_metric`**：在
`_consumer_order_chat` 的 `except StandardAPIError` 分支里补一行

```python
_record_route_metric(context, caller, route_type="qa", outcome="failed", error_code=exc.code)
```

约 3 行，不加新表、不加新系统，拒绝就会出现在 `agent_run_metrics` 里（带租户、带错误码、
带时间），与 QA 行按"同租户 + 同一两秒"对齐。**这不是限流，也不是灰度平台**。

是否在开放前做这一条，是下一轮的判断；不做的话，第九节 9.3 的盲区要在回执里明写。

---

## 十、原始证据位置

| 材料 | 位置 |
|---|---|
| #1015 跨用户拒绝原件（负例响应、停用回读、配置前后、窗口时刻） | `/home/claude/.cache/m3-autonomous/cross-user-denial-20261023/` |
| 正例原件（页面截图、QA 结果、登录态） | `/home/claude/.cache/m3-autonomous/consumer-order-status-20261023/` |
| 只读核查脚本（本片新增） | 同上 `preflight_impact.py`，`0600`，**未入仓** |
| 验收记录 | [`real-h5-status-acceptance.md`](real-h5-status-acceptance.md)（第十三节） |
| 决策材料 | [`permanent-enable-decision.md`](permanent-enable-decision.md) |

公开文档只出现 A/B、O1/O2 与脱敏结论；订单全号、会话、页面原文都在私有目录。
