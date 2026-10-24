# 2026-10-24 · APP-RUNTIME-02：原 App 聊天接入 Dify（真实整链 + 复核修复）

**这一片把「App 聊天 → Dify 已发布 Workflow → 受限本人取数 → 原 GET」接起来并跑通。**
默认关闭，41 与 36 均未改动；公司 App 仓、现役 Dify、节点协议均未改。

运行代码、DSL、测试与证据的完整说明在
[`docs/agents/consumer-order-chat/dify-chat-real-chain-evidence.md`](../agents/consumer-order-chat/dify-chat-real-chain-evidence.md)
与 [ADR-0124](../adr/0124-chat-dify-route-uses-a-one-shot-data-grant.md)。

## 一、新增的自动化检查

| 文件 | 内容 | 结果 |
|---|---|---|
| `tests/test_app_runtime_dify_entry.py` | 绑定表 `engine` 判别（缺省=node、半配拒绝、同场景重复拒绝、未知引擎拒绝）；取数凭据（撤销/过期/一次性/不落明文）；Dify 路线四条成功判据；回调重放不二次读业务；**成功记录过期后回读 410**；**签发后绑定被换掉 ⇒ 回调 403 且零读取**；**没取过数却自称成功 ⇒ failed**；快照是 JSON 文本 / 多一字段 / 非 JSON 三种形状 | 19 passed |
| `tests/test_dify_chat_dsl.py` | DSL 结构：每条边必须写明 `source` 与 `target`；链路形状 start→http→llm→end；取数 URL 是常量；凭据直接绑 `Authorization`；端节点交出快照；模型只拿到取数响应体；**节点 id 字符集** | 7 passed |

**变异验证**（每条都确认删掉修复会转红、还原转绿）：

| 删掉的修复 | 转红的用例 |
|---|---|
| 从 `data` 取 `task_id`（真 Dify 是两层形状） | `test_the_dify_route_delivers_the_same_run_snapshot` |
| 快照 JSON 文本解析 | `test_a_snapshot_returned_as_json_text_is_accepted` |
| 「恰好五个字段」 | `test_a_snapshot_text_with_extra_fields_is_refused` |
| 回读显式 TTL | `test_a_successful_readback_is_refused_after_its_ttl` |
| 回调前的绑定当前性核对 | `test_the_callback_refuses_after_the_binding_is_removed` |
| 成功前要求同轮快照 | `test_a_success_without_this_round_snapshot_is_refused` |
| DSL 边的 `target` | `test_every_edge_names_both_ends`、`test_the_chain_is_start_http_llm_end` |
| 节点 id 用连字符 | `test_node_ids_are_usable_in_prompt_placeholders` |

全量：`uv run pytest -n 4 tests/` → **3780 passed, 9 skipped**。

## 二、真实环境验证

`tools/m2_close/m3node/verify_dify_chat_entry.py` → **28/28 PASS**（2026-10-24T01:22Z）。

真：Dify **1.17.1** 隔离栈（一次性数据根、端口仅回环）、Gateway 真实进程、
`POST/GET /v1/assistant/questions`、Dify 的 HTTP 步骤**自己**带一次性凭据回调取数。
替：模型上游与 CPMS 为回环合成服务；调用者身份 / 订单授权 / 平台目录沿用既有验收启动器的合成替身。

覆盖：主链、指定另一张本人单、两个用户并发不串、上游失败落 failed 且无正文、
取消（终态 + 记录 + 凭据三判）、伪造凭据与无凭据回调两条逆证。

## 三、失败与修复（这一轮真实发生的）

| 现象 | 真因 | 修法 |
|---|---|---|
| 整轮只有 start 执行，导入与写草稿都回 200 | DSL 的边缺 `target`，`graphon._build_edges` 对这类边直接 `continue` | 补齐 `target`，并加 DSL 结构断言（含"存下来的图"核对） |
| `DIFY_WORKFLOW_INVALID: no run identity` | 真 Dify 的 blocking 响应是两层：身份在外层、结果在 `data`；客户端把 `data` 解包后去里面找 `task_id` | 按真实形状分层取值；桩改成照抄真形状 |
| 模型收到字面的 `{{#read-status.body#}}`，工作流仍 succeeded | `graphon` 的 `VARIABLE_PATTERN` 只接受 `[a-zA-Z0-9_]`，节点 id 含连字符时占位符不被解析 | 节点 id 改成 `read_status`，并加字符集断言 |
| `DIFY_NO_DATA`，端节点交回 `"snapshot": "{…}"` | HTTP 步骤的 `body` 永远是文本（`HttpResponse.text`），Jinja2 沙箱也没有 `fromjson` | 网关侧严格解析 JSON 并要求恰好五个字段 |
| 凭据注册 400 `Connection timed out` | 真正连模型上游的是 **plugin_daemon** 容器；宿主 INPUT 策略 DROP，容器到宿主不通 | 给 plugin_daemon 补 `extra_hosts`；验收脚本按端口临时精确放行、结束即删 |

复审后又补了四条（都已加回归）：回调读取期间被取消/过期仍会把数据交出去、
签发后绑定被换掉旧凭据仍可用、没取过数也可能被写成 completed、成功记录回读不看 TTL。

## 四、未完成业务验收

**本次没有真实业务故障案例。** 真实外部模型供应商、真实业务库、真实公司 UPMS 目录、
登录 App 真机验收都**没有**做；生产开关未启用；41 与 36 未部署。
本轮全部结论来自隔离栈 + 合成上游，不能写成真实故障结论。
