# 47. 一个上游一个适配器实例；`reasoning: null` 在转发前丢掉

日期：2026-10-19
状态：已采纳
影响版本：`src/aiops_diagnostics/responses_adapter.py`（`drop_null_reasoning`）、
`.env.example`（适配器一节的登记）

## 背景：备用 provider 接不上，卡在请求形状而不是凭据

ADR 0046 加了"主 provider 故障时换备用"的机制，并把 commandcode 登记为备用。
配好之后复验，**两边都答不出来**，但原因不是余额、也不是 key：

```
Codex turn … failed: {"error":{"message":"Invalid input: expected object,
received null","type":"invalid_request_error","param":"reasoning"}}
```

一路二分到根因：

| 试验 | 结果 |
|---|---|
| 直接打 commandcode `/responses`，**带** `reasoning: null` | **400**（所有模型） |
| 同一请求**省略** `reasoning` | 200 |
| Codex CLI 加 `-m deepseek/…`（CLI 无该模型元数据） | 失败，日志里 CLI 自己打出 `reasoning effort: none` |
| Codex CLI **不加** `-m`（用 CLI 已知模型） | **200，正常回答** |
| baoyun 适配器（8799）带 `reasoning: null` | 200 —— 它容忍 |

即：**Codex CLI 在对自己没有元数据的模型时，会发 `reasoning: null`；commandcode 对这个
形状一律 400。** baoyun 只是恰好容忍，所以单上游部署从来没有暴露过这一点——它不是
"commandcode 有问题"，是**两个上游对这个字段的宽容度不同，而 CLI 的输出形状取决于
它认不认识那个模型**。

## 决策一：把上游差异收敛在适配器里，一个上游一个实例

适配器（`aiops-responses-adapter`）本来就承担"修请求形状 + 补响应 `status`"的职责，
`reasoning: null` 属于同一类：**形状的差异，不是语义的差异**。

- 新增 `drop_null_reasoning`：**只在值为 `null` 时**丢字段。显式的
  `{"effort": "low"}` 是真实指令，原样保留。
- 它**不持有凭据**：客户端 `Authorization` 头原样透传，key 仍由 Gateway 的
  `key_slot` 文件提供。所以多起一个实例不需要多管一份秘密。
- 实例之间只差 `PORT` 与 `UPSTREAM`；`.env.example` 补了此前**完全没有登记**的
  适配器一节（这正是它长期只存在于 41 主机、仓库里查不到的原因）。

## 决策二：`deepseek-v4.1-flash` 走 commandcode

实测两边对同一个模型名的支持是**互斥**的：

| 上游 | `deepseek-v4.1-flash` | `deepseek-v4-flash` |
|---|---|---|
| baoyun（直连） | **503**（不支持） | 200（上游实际返回 `deepseek-v4-flash-ga-260731`） |
| commandcode | **200** | 200 |

需求要用的模型是 `deepseek-v4.1-flash`，而 baoyun 不提供它。所以 commandcode 不能
只当备用——**它必须是主 provider**，baoyun 退为备用（等它充值后对 `v4-flash` 仍然可用）。

## 被否掉的替代形态

- **改 Codex CLI 的调用方式让它别发 `reasoning`。** CLI 的行为取决于模型元数据，
  不是本仓可控的开关；而且要覆盖的是"所有对上游形状的容忍度差异"，逐个绕会一直漏。

- **在 Gateway 侧改请求体。** Gateway 用 SDK 驱动 CLI，请求体是 CLI 自己组装的，
  构造点在 SDK 内部——那里没有我们的接缝。适配器是唯一合法的位置。

- **把 `reasoning` 换成空对象 `{}`。** 会把"没有推理配置"谎报成"有推理配置"，
  而空对象在某些上游是另一种非法的形状。丢字段是如实表达"客户端没有给这一项"。

- **给每个上游在代码里写一个 `if provider == ...` 分支。** 把上游的怪癖写进运行时，
  换上游就要改代码；适配器实例是配置，不是代码。

## 后果

更容易：接一个新的 Responses 上游 = 起一个适配器实例 + 在 provider 表里加一行，
不用改任何运行时逻辑。上游之间对请求形状的宽容度差异被收敛在**一个**有测试的模块里。

更难：**一台上游一个实例**，实例多了要各自起 systemd 单元并登记端口；
端口从 41 的主机端口登记表里分配，不能随手挑。

上限：适配器只修**已知**的形状差异。一个没见过的上游怪癖仍然会以原始报错的形式
冒出来——这是刻意的：`patch_body` 对看不懂的 body 原样转发，不去猜。
