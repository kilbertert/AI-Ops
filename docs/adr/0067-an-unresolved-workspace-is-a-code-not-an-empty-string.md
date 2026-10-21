# 67. 解析不到 workspace 必须是一个码，不是一个空串

日期：2026-10-21
状态：已采纳（#879 实施；ADR-0059 的 §5.1 落成代码）
影响版本：`src/aiops_diagnostics/dify_workspaces.py`（新）、
`src/aiops_diagnostics/deployment_environment.py`（新）、
`src/aiops_diagnostics/gateway_config.py`（`AIOPS_GATEWAY_ENVIRONMENT`、内部令牌）、
`src/aiops_diagnostics/gateway_api.py`（`GET /v1/dify/workspace`）、
`src/aiops_diagnostics/dify_login_provision.py`（环境判据改为共用一份）

## 背景

ADR-0059 决定「一租户一 workspace，按 `(tenant_id, environment)` 解析」，但没有规定
**解析失败长什么样**。这一张票把那个形状定下来，因为它是后面四张票（开户、发布桥、
试运行、运维 CLI）共用的接缝，而接缝的形状一旦被三个调用方各自猜一次，就会漂成三种。

要躲开的正是这个仓已经见过的形态：`mem-20261019-ranlei-002` 里**同名不同物**的绑定
制造出"比 null 更糟的语义错误"。这里是它的亲戚 —— 「没有值」与「值是空」在 Dify 上
**含义相反**：`workspace_id` 为空串在 Dify 那边的语义是平台根，照它去读写就是打到
别人的工作区。

## 决策

**`resolve_workspace` 只产出两样东西：一个非空 `workspace_id`，或一个带稳定码的异常。
没有空串，没有 `None`，没有第四种。**

| 状态 | 码 | 状态码 | 调用方该做什么 |
|---|---|---|---|
| 缺行 / 行不可用 | `DIFY_WORKSPACE_UNRESOLVED` | 404 | 去开户，别等 |
| `creating` | `DIFY_WORKSPACE_CREATING` | 409 | 稍后重试（`retryable=true`） |
| `failed` | `DIFY_WORKSPACE_FAILED` | 409 | 重建；消息里带上次的失败原因 |
| `unknown` | `DIFY_WORKSPACE_UNKNOWN` | 409 | **先对账**，别重建 |

`created` 与 `ready` 都返回 id：它们回答的是同一个问题（"是哪一个"），区别只在后续
步骤（插件、凭据、模板 DSL）跑没跑完，那是 #881 的事。

**状态→HTTP 的状态映射只有一份**（`DifyWorkspaceError.http_status`）。网关的只读端点
照抄它，未来的进程内调用方也读它 —— 两处各写一遍 `if state == ...` 是漂移的起点。

**环境只来自部署身份，永不来自请求。** `GET /v1/dify/workspace` 请求里带
`environment` 参数一律 400，且**不**回退到部署身份：只忽略它会让调用方以为自己拿到了
另一个环境的答案，而"跨环境查同一个 `tenant_id`"正是这条端点的越权口。

**判据只有一处实现。** 新模块 `deployment_environment.canonical_environment` 同时供
网关（`AIOPS_GATEWAY_ENVIRONMENT`）与登录代理（`AIOPS_LOGIN_PROXY_ENVIRONMENT`）使用；
登录代理那一侧只把 `ValueError` 包成自己的 `DifyProvisionError`。

**必备性在起监听那一刻判，不在 `from_env()` 里判。** 这一条是评审期间改的：把
`canonical_environment` 放进 `from_env()` 会让 24 处测试调用（`test_config.py`、
`test_routing.py`、`test_company_token_auth.py` 等）在没有部署身份的情况下全部失败，
而它们测的是别的东西。区分点很清楚 —— **一个直接构造出来的 settings 不是一次部署，
`serve` 才是**。所以 `validate()` 只判格式（空值合法且意味着"这个进程没有部署身份"，
用到它的端点因此不启用），`gateway_server.serve` 在起监听前判非空。

## 被否掉的替代形态

- **解析函数返回 `""` / `None`，由调用方判断。** 这正是要消掉的形状：三个调用方会
  各自发明一种判断，而它们对"CESTING 要不要重试"的答案会不一致。而且空串在 Dify 一侧
  有个**非空语义**（平台根），返回它是给出一个会被误用的值。
- **解析函数顺手建一个（缺行就开户）。** ADR-0059 已经把"建"限定在开户那一步。让解析
  会建，意味着发布桥或运维 CLI 跑一次就可能永久造出一个 workspace —— 而 Dify
  **没有删 workspace 的 API**，孤儿不可回收。
- **`creating` 直接报 `UNRESOLVED`。** 两者对调用方的指令相反：前者是"等"，后者是
  "去建"。合并之后，一次正常的并发开户会让第二个调用方也跑去建。
- **把 `environment` 放在请求参数里（可选、缺省用部署身份）。** 那会让"跨环境查同一个
  `tenant_id`"变成调用方的自由选择，而 `environment` 列存在的全部意义就是阻止这件事。
- **在 `from_env()` 里判必备性。** 见上面"必备性"一段：它把"一次部署"与"一个配置对象"
  混成同一件事，代价是 24 处与部署身份无关的测试要各自补一个假环境。
- **给这条端点再开一把 Dify 侧钥匙。** 调用方是我们自己的另外两个进程，不是 Dify；
  而 Dify 侧钥匙的设计前提是"泄露了也拿不到东西"，这条端点回的是某个租户的 workspace
  id，那个前提不成立。改用进程间内部令牌（HMAC + 时间窗，与平台
  `InternalTokenManager` 同格式），且它不进任何面向 Dify 的配置。

## 后果与代价

- **调用方多一次回环调用**（代理 → 网关），这是 ADR-0059 已接受的代价：一份真值胜过
  三份私有表。
- **`AIOPS_GATEWAY_ENVIRONMENT` 是新的启动前置。** 41 与 36 部署时必须先写这一行，
  否则网关**起不来**。这被刻意选成"起不来"而不是"起得来但少一个能力"——后者会在第一次
  开户时才暴露，而那时表现成"某个租户的工作区找不到"，与配置无关。
- **`created` 与 `ready` 在解析上等价**是临时的：#881 让 `ready` 成为真正的终态之后，
  如果某个调用方需要区分，它得问 `DifyWorkspaces` 拿整行，而不是让 `resolve` 再涨一个
  返回值。
- **`dify_workspaces` 落在 `gateway.db` 而不是代理的库**（ADR-0059 已定），因此代理必须
  有网关的内部令牌。这个令牌的轮换是运行手册的一部分，不是代码。
