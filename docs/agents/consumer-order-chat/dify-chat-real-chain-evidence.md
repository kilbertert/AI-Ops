> **2026-10-24 续办**：固定发布版本与运行中 stop 扩展验收 **32/32 PASS**。
> 见[本轮记录](../../validation/2026-10-24-dify-version-cancel.md)。以下 27 项是此前那轮结果。

# 真实 Dify 整链证据 · APP-RUNTIME-02

**结果：27/27 PASS**（`status: PASS`，`failures: 0`）
**时间**：2026-10-24T01:11:35Z 起，一次性数据根，模型与 CPMS 为合成上游
**跑法**：`uv run python tools/experiments/m2_close/m3node/verify_dify_chat_entry.py --out <file> --keep`

## 这一轮里什么是真的、什么是替的

| 真 | 替 |
|---|---|
| Dify **1.17.1** 的编排、HTTP 步骤、LLM 步骤、发布与运行 | 模型上游：本机回环的 OpenAI-compatible 合成服务 |
| Gateway 的绑定选择、选单、授权、取数凭据、回调、放行 | CPMS：本机回环桩 |
| `POST` / `GET /v1/assistant/questions` | 调用者身份：`gateway_launcher` 的固定令牌 |
| Dify 的 HTTP 步骤**自己**回调网关取数 | 订单授权表 / 平台目录：合成数据 |

**关键一点**：取数这一步是 Dify 容器里的 HTTP 节点发出的，脚本没有代发、也没有 mock
Dify 的结果。凭据由 workflow 输入直接绑在 `Authorization` 头上。

## 判据（27 条，逐条可观察）

| 组 | 判据 |
|---|---|
| 栈 | 隔离栈起来、`/health` 就绪、模型 provider 可用、容器→宿主两端口精确放行、真 Gateway 就绪 |
| Dify 配置 | init validate、console setup、console login、import dsl、装 provider 插件、provider ready、注册模型凭据、write draft graph（含**存下来的图**核对）、拿到已发布 workflow + service key、绑定后 Gateway 再次就绪 |
| 主链 | App POST 202 且早于 Dify 完成；原 GET 到 completed 且正文含本轮快照订单号；执行记录有真 `workflow_run_id` + `task_id` 且 succeeded；业务**只读一次**；模型输入不含取数凭据；完成之后凭据被撤销 |
| 另外的订单 | 指定本人另一张单：读的就是那一单 |
| 并发 | 两个不同用户并发，各自只拿到自己的单 |
| 失败 | 上游失败 ⇒ failed 且无正文（**不换引擎**） |
| 取消 | 用户取消 ⇒ 终态不是 completed；执行记录不是 succeeded；该次凭据已撤销 |
| 逆证 | 伪造凭据被拒且 0 次业务读取；无凭据回调被拒且 0 次业务读取 |

## 直接观察到的三条关键事实

1. **业务只读一次**：`CpmsHandler.calls == ["ORDER-3001"]`。
2. **模型看不到凭据**：播报步骤收到的提示里没有 `cdg_`、没有 `Bearer`、没有 `Authorization`；
   只有取数步骤交回的、已裁剪成五个字段的 JSON。
3. **取消之后凭据失效**：该 QA 名下 `chat_data_grants.state == "revoked"`。

## 复跑需要什么

- Docker 可用（拉 `langgenius/dify-api:1.17.1`、postgres、redis、weaviate、
  `dify-plugin-daemon:0.6.10-local`），首次要装一次模型 provider 插件（约 4 分钟等待）。
- 验收 driver、Dify、Gateway 与合成上游共用一次性容器网络命名空间，宿主不发布端口、
  不改防火墙。脚本使用本次私有目录；`--keep` 保留证据和卷，不操作现役实例。
