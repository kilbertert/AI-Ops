# 78. `ensure-ready` 写完凭据还必须把它切到**生效位**

日期：2026-10-22
状态：已采纳（#881 的 41 真机验收发现；ADR-0076 的第三处修正）
影响版本：`src/aiops_diagnostics/dify_console_admin.py`（`active_credential_name` /
`activate_provider_credential`）、`src/aiops_diagnostics/dify_workspace_ready.py`

## 背景

ADR-0076 说"写该 workspace 自己的 provider 凭据，已存在同名则跳过"。这句在**全新**
workspace 里成立：那里只有一份凭据，写进去就是它。

在**迁移过的** workspace 里不成立。2026-10-22 在 41 的租户 `1942105476598861824` 上实测：

```
ensure-ready → state: ready, credential_written: true
chat         → 400 PluginInvokeError ... 401 Authentication Fails, Your api key: ****eiKb is invalid
```

那个 workspace 里有两份 deepseek 凭据：本次写的 `aiops-workspace`（key 有效）与迁移时从旧
workspace 逐列复制来的 `deepseek-official`（**key 已失效**）。Dify 的"当前凭据"按
`ProviderCredential.created_at DESC` 选，所以旧的那份仍排在前。切换后对话立刻 200。

## 决策

**"凭据存在"与"凭据生效"是两件事，`ready` 必须以后者为准。**

`ensure_ready` 在写完凭据之后**回读** `custom_configuration.current_credential_name`；不是
`aiops-workspace` 就切过去（`POST …/<provider>/credentials/switch`，body
`{"credential_id": …}`）；已经是它就是零写。`ReadyOutcome` 增加 `credential_activated`，
让"这次切了"与"本来就在位"可区分 —— 否则一个每次都在切的状态看起来与幂等没有区别。

## 被否掉的替代形态

- **删掉旧凭据**。迁移带来的 `deepseek-official` 是旧 workspace 的**回滚点**的一部分；删它
  等于在"新链路还没跑过真实发布"的时候就拆掉退路（与 ADR-0059 §5.1b 的六步顺序冲突）。
- **按 `created_at` 自己挑最新的一条**。那是复述 Dify 的排序规则；下一次 Dify 改排序，
  我们就静默挑错。**回读名字**问的是"现在生效的是不是我"，与它怎么排序无关。
- **让运维在控制台点一下**。这是把一个可判定的状态变成人的记忆；而且（本轮实测）它只在
  **迁移过的** workspace 里出问题，人不会知道该点哪一个。

## 后果与代价

- **多两次读 + 一次写**（读 provider 列表、比对、必要时 switch）。清单极短的 workspace 里
  这一步只有读。
- **`ready` 现在蕴含"凭据生效"**，但**不蕴含"模型可用"**：key 仍可能被供应商禁用
  （`API-key is blocked` 是 41 上真实出现过的形态）。那要靠一次真实调用才能发现，
  `ensure-ready` 不做模型调用。
- 这条修正在**全新** workspace 上是空操作（唯一的凭据本来就是当前），所以它只在迁移路径
  上有效果 —— 也正因为如此，单测必须显式构造"旧凭据仍是 current"的场景，否则用例会空转。
