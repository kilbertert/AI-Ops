# 41 原应用只读问答验收（2026-10-31）

用户明确授权：**在生产 Dify 应用上做只读问答验收**。本轮未做任何写操作（未改提示词、
未发布、未建/删应用、未构造 app）。

## 一、生产拓扑（只读取得）

| 项 | 值 |
|---|---|
| 客户端应用 | `394b5399-7ad9-428e-b2b0-cc7f05720a81`（`wf01-consumer-service`，144 次运行） |
| 管家应用 | `2b0629cf-bb84-5e1f-bdd8-8b1da2ce9053` |
| 生产 Dify | `http://36.156.159.175:10008`，inner API 经 `127.0.0.1:15001` 隧道（unit active） |
| 网关 | 41 上 `1.0.0+4ec55c7a1c0c`，active |
| 工作区 | 三个租户均 `setup_state=ready` |

## 二、只读探测结果

真实用户会话（thirdSession 由网关自己的 Redis 选，值不打印）：

```
✅ zh  /v1/faq/recommendations  HTTP 200  served=zh  n=28
```

判据取「200 且 served 等于请求语言且非空」。

## 三、生产实际生效的契约版本 —— 本条是本次最重要的发现

最近一次成功的真实 Dify 运行（`chat_dify_runs`，2026-10-30T10:50Z）：

- `context_json` 里 **`mode=documents`**（documents 契约在跑）
- 但 **没有 `fact_contract` 字段**，`reply_source=null`

按 `consumer_knowledge.py`：

```python
result["fact_contract"] = "slots" if binding.callback_version >= 5 else "legacy"
```

缺少该字段意味着当时走的是 **`legacy` 分支**（弱判据），而不是新的事实片段契约（`slots`）。

**因此：#1232 以及后续守卫修复（`4ec55c7a`）虽然已部署到 41，但生产原应用尚未启用事实片段契约。**
启用它需要在应用侧把 `answer_fact_contract=slots` 环境变量写进 workflow 并重新发布
（`consumer_flow_contract._fact_slots` 的判据）—— 那是一次**写操作**，不在本次授权范围内。

这也解释了为什么海量单测与 32 次样本都能通过，而生产没有暴露契约问题：**生产线还在旧契约上**。

## 四、⚠️ 订正：控制台凭据**没有**失效（我上一版判断错了）

本文最初版本断言 `AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY` 失效（401）。**那是错的**，
而且错在两处，值得记下来：

1. **漏了必需的 `X-WORKSPACE-ID` 头**。`dify_console_admin._headers` 同时发
   `Authorization: Bearer <key>` **和** `X-WORKSPACE-ID: <workspace>`；我只带了前者，
   于是 401 —— 一个缺失的 header 看起来和一把失效的密钥一模一样。
   带上正确 workspace 后 `/console/api/apps` 返回 **200**。
2. **用错了 workspace**。我先用了 `AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID`（`2db44904…`），
   该 workspace 里没有生产应用，于是拿到 404 —— 看起来又像"应用不存在"。
   生产租户 `1942105476598861824` 的真实 workspace 是
   `c3f871e1-ffd0-4a10-a84a-b8696f7acd25`（来自 `dify_workspaces` 表）。

两次都是**我的调用方式**错了，不是生产环境坏了。已在本文更正，并把正确的只读读取器
落在 `tools/verification/inspect_production_prompt.py`。

未验证是否为"密钥被轮换"或"Dify 侧账号变更"。**未做任何修复**，也不尝试绕过。
这独立于本次需求，但会阻塞"运营在页面上改提示词"这条产品路径 —— 须单独处理。

## 五、边界：本轮**没有**验证到的

- **没有**通过生产 Dify 应用发起一次真实提问并观察模型回复。原因：该路径需要已签发的
  service token 经 `/v1/assistant/questions` 触发，属于会创建 `qa` 记录与 Dify run 的写路径，
  超出"只读"授权。已做的是只读探测 + 生产运行记录复核。
- **没有**验证生产上的提示词编辑/预览/发布/恢复 —— 那是写操作，且当前 console 凭据失效，双重不可行。

## 六、结论

| 项 | 状态 |
|---|---|
| 生产网关可用、真实用户只读访问 | ✅ 通过 |
| 生产 Dify 应用在运行（documents 契约） | ✅ 已确认（记录层面） |
| **生产启用事实片段契约（`slots`）** | ❌ **未启用**，需一次写操作 |
| 生产控制台凭据 | ❌ 401 失效 |
| 生产页面级编辑/发布/恢复 | 未验（需写授权 + 凭据修复） |

**不宣称 41 原应用验收通过。** 本轮只证明了只读面可用，并查清了两件此前未记录的事实：
生产线仍走旧契约、控制台凭据已失效。
