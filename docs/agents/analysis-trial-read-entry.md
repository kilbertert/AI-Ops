# 运维分析与试运行：读哪个入口、scope 怎么隔离

**一句话**：`standard_diagnoses` 一张表装三种作业（`kind = order / analysis / trial`），
它们**按 `scope_fingerprint` 隔离**，指纹不同就是 404，和"这个 id 不存在"完全一样。
所以「订正了状态却还读不到」通常不是数据问题，是**入口与身份口径**问题。

## 读取入口（凭据与入口都不同，不能互换）

| `kind` | id 前缀 | 读取端点 | 凭据 / 入口 |
|---|---|---|---|
| `order` | `dx_` | `GET /v1/standard/diagnoses/{id}` | third-session 或公司令牌；`X-Business-Entry` 按端 |
| `analysis` | `an_` | `GET /v1/standard/analyses/{id}` | **必须 `operator` 入口**，否则 403 |
| `trial` | `tr_` | `GET /v1/dify/trial/{id}` | Dify trial 凭据 + **发起人头**（见下） |

`analysis` 的入口检查是**写死**的（`gateway_api.get_operations_analysis`）：

```python
if str(decision.platform) != PLATFORM_OPERATOR:
    raise StandardAPIError(403, "OPERATOR_ENTRY_REQUIRED", "operator entry required")
```

拿 consumer 会话或 `/v1/standard/diagnoses/{an_...}` 去查，**必然查不到** —— 不是 bug。

## trial 的两种口径，取决于「桥有没有给发起人」

`_trial_run_context()` 是唯一决定点，**提交与读取必须走同一个函数**（代码注释里
把它写成"v2 地雷"：提交用 A 范围、读取用 B 范围时，返回的 `trial_id` 永远读不回来，
且表现与"这个 id 不存在"一模一样）。

| 情况 | 口径 | 可见范围 |
|---|---|---|
| 桥给了 `X-AIOps-Initiator` + `-Tenant` + `-Type` | `trial_context_for(initiator)` | 发起人本人在管家的站点范围（`organ`） |
| 桥没给（或 `initiator="unknown"`） | `trial_run_context()` | 固定调试租户，顶层账号口径（`all`） |

调试租户来自 `AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT`。41 上实测该值算出的指纹
与库里 `initiator=unknown` 的那一批 trial 行**逐字一致** —— 这就是它们该走的入口。

## 41 上的实测结论（2026-10-23）

订正 94 条被错误 sweep 的 `expired` 行之后，按 `kind` 复核可读性：

| kind | 条数 | 用对应入口能读到 | 说明 |
|---|---|---|---|
| `order` | 54 | **54** ✅ | 其中 10 条属于当前 consumer 会话口径 |
| `trial` | 5 | **3** | 3 条是 `initiator=unknown` → 调试租户口径 |
| `analysis` | 36 | **0** | 见下 |

`analysis` 全部读不回来，原因是 **scope 无法复现**，不是订正失败：

- 早期的 `tenant_id` 是 `NULL`（租户列后加的历史行）
- 指纹含**当时的运营商站点集合**，而 41 现在的店铺绑定已经变了
- 同一个 `7268e416…` 同时出现在 `tenant=NULL` 与 `tenant=1942…` 两个时期 ——
  说明它按**人**算，不按租户算

**不要为了"让历史行可读"去伪造 scope。** 那等于绕开授权边界，而且这类行是
开发/验收期产物，不是用户现在会打开的作业。数据本身是完整的：用 store 的读路径
（带每行自己的 `scope_fingerprint`）逐条验证过，**95/95 都能读出结果**。

## 可复用的判别顺序

判断"某条作业行为什么读不到"：

```bash
# 1. 它是哪种 kind，决定端点和凭据
sqlite3 gateway.db "select kind from standard_diagnoses where diagnosis_id='<id>'"

# 2. 它的 scope_fingerprint 是什么
sqlite3 gateway.db "select scope_fingerprint, tenant_id from standard_diagnoses where diagnosis_id='<id>'"

# 3. 用**代码路径**算出目标入口此刻的指纹，与第 2 步逐字比对：
#    order    -> 当前会话的 ScopeContext.scope_fingerprint
#    analysis -> operator 入口的 ScopeContext.scope_fingerprint
#    trial    -> _trial_run_context(initiator) 或 trial_run_context()

# 4. 相等 => 该入口应读得到；不等 => 那是授权边界，不是数据损坏
```

第 3 步必须**用代码算**，不要手抄 JSON 反推 —— `_scope_fingerprint` 的字段
（`caller_b_user_id` / `subject_b_user_id` / `subject_c_user_id` / `subject_user_type`
/ `delegated` / `effective_tenant_id` / `data_scope` / `roles`）每一个都会改变哈希，
`user_type` 还是数字编码（`-1` 平台 / `1` 租户 / `5` 代理商），猜错的概率极高。
