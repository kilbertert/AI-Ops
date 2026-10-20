# 60. Dify 账号邮箱带环境前缀：挡的是两套 UPMS 撞号串账号

日期：2026-10-20
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_login_provision.py`（`account_email_for` 加
`environment`、`DifyConsoleSettings.environment` 必填）、
`src/aiops_diagnostics/dify_login_server.py`（`AIOPS_LOGIN_PROXY_ENVIRONMENT` 必填）、
`src/aiops_diagnostics/dify_login_service.py`（`MappingStore.retag_email` + 登录时的重标）

## 背景：账号是全局的，邮箱是它唯一的身份

Dify 账号不属于任何 workspace —— 它是实例级的，身份由**邮箱**唯一确定
（`accounts.email`）。而 41 与 95 是两套**独立 UPMS、独立数据库**，`sys_user.id` 各自生成。

现有派生只看 id：

```python
def account_email_for(company_user_id: str) -> str:
    return f"{safe}@{ACCOUNT_EMAIL_DOMAIN}"  # ← 没有环境
```

于是两套环境里 `id` 相同（但人不同）的两个用户，会得到**同一个邮箱 ⇒ 同一个 Dify 账号**。
后果不是"登录失败"，是**越权**：95 的代理为这个账号签会话，那个人就能进 41 那个人的
workspace —— 看到并编辑他的智能体。

这条缺陷在**今天的代码里就已经成立**，只是因为目前只有一个环境、撞号还没发生过。
它与多 workspace 无关：多 workspace 隔离的是 workspace，账号仍然是全局的。

## 决策：邮箱 = `{environment}-{company_user_id}@{domain}`

`environment` 是**部署身份**（每台机器一个值，`AIOPS_LOGIN_PROXY_ENVIRONMENT`），
**必填**，不从请求读。

必填而不是给默认值，是这个决定里最要紧的一半：

- 给一个默认值（比如 `"default"`），漏配的部署会**静默退化成旧行为**——两个环境都用
  `default-`，撞号照旧，而启动、登录、审计全都正常；
- 必填 ⇒ 漏配就起不来。这是"宁可起不来，也不要悄悄少一层隔离"。

空串/纯分隔符在**两个入口**都拒：`account_email_for` 本身，以及 `DifyConsoleSettings`
的构造（更早、更容易被发现）。

## 已有映射怎么迁：只改邮箱列，不换账号

**Dify 1.17.1 没有改账号邮箱的端点**（2026-10-20 用户确认）。所以"把旧账号的邮箱改成
带前缀的"这条路不存在。剩下两条：

| 做法 | 后果 |
|---|---|
| 按新邮箱**再开一个** Dify 账号，映射指过去 | 旧账号上的 `apps.created_by`、`app_model_configs`、审计行**全部失去归属**（那些表记的是账号 id，不是邮箱） |
| **只改映射表的 `dify_email` 列**，`dify_account_id` 不动 | 什么也不影响 —— 见下 |

选后者。理由是**没有任何一条身份路径经过邮箱**：

- 会话是 `mint_console_session(account_id)` 按**账号 id** 签的；
- `by_dify_account` 按 id 反查；
- `provision_editor` 只在 `account_id` 为空时才走邀请/激活。

所以 `dify_email` 这一列在运行时唯一的用途是**记录**（与人读）；改它不改变任何行为。

**为什么不撞号**：Dify 里那个旧账号保留着无前缀的旧邮箱，而**所有新派生的邮箱都带前缀**，
两个值永远不可能相等。所以"旧账号 + 新账号"不会互相顶替。

**重标发生在登录时**：`LoginService.login` 发现 `existing["dify_email"]` 与派生值不同
就调 `retag_email` 并记一条 `email_retagged` 审计。这比一个独立的迁移命令更可靠 ——
它只在真正有人在用那个账号时发生，而且不需要运维记得去跑。

代价（明写）：**Dify 侧那个账号的邮箱仍是旧值**。于是"从 Dify 成员列表按邮箱找我们的人"
这条路对旧账号不成立 —— 实际上它本来也不成立（`find_account_id` 只在开户时用，且用当时
派生出的邮箱）。这条代价记录在案，不建议为此去动 Dify 的账号。

## 被否掉的替代形态

- **给 `environment` 一个默认值**（比如 `""` 或 `"default"`）。见上：漏配会静默退化，
  而这层隔离的全部价值就在于"它一定在"。
- **用一个哈希把 `(environment, id)` 压成一个短值**。邮箱会变得不可读，而可读性在这里
  是运维排查的入口（`aiops-login.invalid` 域下一眼看出是谁）。不做哈希也没有碰撞风险：
  输入空间就是 `sys_user.id`，本身是唯一键。
- **在 Dify 里给每个环境建独立 workspace 来隔离**。workspace 隔的是**内容**，不是**账号**：
  同一个 Dify 账号可以被加进多个 workspace，所以它挡不住"两个环境用到同一个账号"。
- **等 Dify 支持改邮箱再迁**。那等于把一条已知越权路径留在一个未知时长里。

## 后果与代价

- **每次部署多一个必填项**。漏配的失败是**启动即失败**（fail-closed），不是运行中出错。
- **旧映射的 Dify 账号保留旧邮箱**（见上）。这是本决定唯一没有做干净的地方，明写在此。
- **两个环境不再可能共用 Dify 账号** —— 这是这一条的全部目的。
- **与多 workspace 的关系**：它是多工作区实施的**前置**。多 workspace 让"每租户一个
  workspace"成立，但账号层仍然是全局的；这条不修，多 workspace 只是把越权范围从一个
  workspace 扩到 N 个。
