# 2026-10-24 · APP-RUNTIME-01 续办：接线修复与真 Java 节点整链

**原 #1046 只交付了接线的一半；这一片把它补成任务书要求的样子。**
独立复核在 `/home/claude/.cache/m3-autonomous/REVIEW-2026-10-24-app-runtime-node-entry-codex.md`，
五个反例逐条对应下面五节。

## 一、复核的五项必修（都已修，每条都有能检出它的回归）

| # | 复核结论 | 修法 | 回归（变异即转红） |
|---|---|---|---|
| 1 | `resolve_binding` 只读全局文件，**未登记租户照样派到同一个节点** | 绑定改成一张登记表（`aiops-chat-execution/1`），键＝（环境，租户，入口，场景）；选中那条的钥匙 `chat_binding` 冻进 run，回调与回读按它找回**签发时那一条**；未登记租户走原直读且**一次派发都不发生** | `test_unregistered_tenant_keeps_the_direct_read_and_dispatches_nothing`、`test_two_tenants_are_dispatched_to_their_own_nodes`（去掉租户判据 ⇒ 2 条转红） |
| 2 | 缺清单也能**提前假完成**：`load("")` 返回 `None`，`complete()` 按旧协议直接转 `succeeded` | 绑定加载期与签发期各拒一次；聊天路线的 run **必须**带期望身份 | `test_a_binding_that_points_at_no_manifest_is_refused_at_validation`、`test_issue_refuses_a_run_without_a_manifest_even_if_a_binding_says_so`、`test_claim_without_completion_can_never_succeed` |
| 3 | 聊天 GET 另写一套判据，**漏了四项**（settings 环境、actual==expected、节点登记环境、执行配置是否核对过） | 抽 `chat_execution.final_release`，委派入口 / 聊天 worker / 聊天回读**共用一份**；另加"缓存正文必须就是该快照的渲染" | `test_answer_stops_when_the_recorded_environment_no_longer_matches`、`test_answer_stops_when_the_stored_exec_config_is_mismatched`、`test_answer_stops_when_the_snapshot_came_from_another_node`、`test_a_tampered_cached_answer_is_not_delivered` |
| 4 | 停用后**连无正文终态也读不到**：取消返回 404 而库里已 `cancelled` | 停用只拒绝**正文**；`queued/running/failed/cancelled/expired` 与绑定是否启用无关 | `test_disabling_the_route_hides_the_body_but_keeps_the_status_readable` |
| 5 | 测试失真：patch 错模块、`_identity(Path)` 被传 `str`、非终态用例只断言 403/404、取消靠 sleep 赛跑 | 全部改正；漂移用例断言**真的**发出了坏摘要且收到 `AGENT_RUN_EXEC_CONFIG_DRIFT`；取消用事件握手钉住派发/领取/迟到完成 | 见"反向验证"一节 |

## 二、真实 Java 节点整链（原任务硬条件，本片补齐）

`tools/m2_close/m3node/verify_chat_node_entry.py`，**18/18 判据通过**。

```
App POST /v1/assistant/questions → 真 Gateway → 按租户选绑定 → 选单 → 授权 → 签发
  → 编排平台替身起一个**真 Java 节点** → 节点自己领取 → 自己跑两轮模型工具循环
  → 自己发完成通知 → 同一 run 的 succeeded 快照 → 原 GET 拿到 blocks
```

| 判据 | 实测 |
|---|---|
| POST 202 且**早于**节点完成 | 11 ms 返回 `queued` |
| GET 到 `completed`，正文含该 run 快照的订单号 | `ORDER-1001` |
| run 由真节点领取并整轮完成 | `succeeded` + `exec_config_state=matched` + `actual_node_id=node-local` |
| QA↔run 存储关联 | `agent_runs.qa_id` 反查命中 |
| 业务只读一次 / 派发一次 / 模型两轮 | `["ORDER-1001"]` / 1 / 2 |
| 指定另一张本人订单 | 两条请求分别选到 `ORDER-1001` 与 `ORDER-1002`，各自一次读、各自一次派发 |
| **未登记租户** | 不产生 run；仍拿到本人直读答案（原分支未被打坏） |
| 第二轮模型 500 | QA `failed` 无正文；run 不 succeeded |
| 末轮截断 | 同上；业务只被读了领取那一次（无直读回落） |

替身（都写进报告）：模型上游、CPMS、编排平台、调用者身份、订单授权、选单的**数据源**、
平台目录。**领取与完成通知由节点自己发出** —— 没有 Python 派发器代劳。

## 三、反向验证（逐条实跑，不重跑赌时序）

| 变异 | 期望 | 实测转红 |
|---|---|---|
| 绑定不按租户 | 未登记租户被派发 | **2** 条 |
| 签发不要求清单 | 缺清单的 run 仍签发 | **1** 条 |
| 绑定加载不要求清单 | 空清单通过校验 | **1** 条 |
| 回读不校验缓存正文 | 被改过的正文照发 | **1** 条 |
| 放行不查执行配置核对 | `mismatched` 仍交付 | **1** 条 |
| 放行不查 `actual == expected` | 另一台活节点交出的快照被放行 | **1** 条 |
| 节点失败回落直读 | 失败用例拿到答案 | **3** 条 |

## 四、回归

```
uv run pytest -n 0 tests/test_consumer_order_chat.py tests/test_m3_consumer_snapshot.py tests/test_agent_run_records.py
98 passed          # 原基线，一条未减
uv run pytest -n 0 tests/test_app_runtime_node_entry.py
27 passed          # 本片（原 16 条中的失真用例已改正，不是删掉）
uv run pytest -n 0            # 全量
3733 passed, 9 skipped, 0 failed
```

## 五、过程中抓到的三个真缺陷

1. **验收入口选错**：harness 轮询不带 `X-Business-Entry`，于是每次 GET 都 403，
   每个场景都要等到超时才落终态 —— 看起来像"节点没跑完"，其实是调用方没声明入口。
2. **夹具把订单号当主键用错**：第二个租户的主体复用了 `ORDER-1001`，
   `INSERT OR REPLACE` 把它从 USER-A 手里换走，于是"最新一单"变成了另一张单，
   CHAT-03 以一个错误的理由通过。
3. **`git checkout --` 回滚变异时把整个文件抄回去了**（与上一个 M3 片同一个坑）：
   变异脚本改成"先把原文读进内存、跑完再写回"，不再用 `git checkout`。

## 六、仍未完成（不得写成已完成）

1. **真实外部平台/供应商**：未接，模型与 CPMS 都是本机合成；
2. **真实公司 UPMS 目录**：本片用只认 consumer 的目录替身；
3. **登录 App 真机验收**：未做；
4. **执行配置身份 ≠ 运营 SOP 发布绑定**：后者明确留待 M3，本片不造版本；
5. **生产未启用**：`AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE` 默认空，41 上不存在这个键。

## 七、生产影响

本片**不改公司仓、不手工部署/重启/启用、不调真实订单或供应商**。

需要区分的两件事，不能都写成"零影响"：

- **手工未启用**：默认空 ⇒ 新路线不可达，行为与接线前逐字一致；
- **正常 CD**：合并后会构建并部署新镜像。本片给 `agent_runs` 加了 `chat_binding` 一列
  （幂等 `ALTER TABLE`，旧行 NULL 不回填），并更新了 `images.lock.json` 的上下文摘要 ——
  这两项**确实会**在部署时发生，不是"零改动"。回滚路径：清空绑定键即回到原直读；
  库里的新列不影响旧代码读取。

## 八、续办（Codex 复核 9fffdf7 之后）：三个运行缺陷

复核在 `9fffdf7` 上复现了三个**运行**缺陷；本片逐条修复，并处理与最新 main 的冲突。

| 线程 | 缺陷（复核实测） | 修法 | 回归（变异即转红） |
|---|---|---|---|
| P1 `…vv5li` | `final_release` 读配置之后不重读 run；配置读取期间撤销/到期被漏掉，GET 仍 `200 + 正文` | `get → 读绑定/配置 → **再 get**`；只用第二次读到的对象判定与返回。复用既有 `store.get`（读事务，顺带收敛过期），**不加锁、不加分布式协议** | 撤销 / 到期 各 1 条 |
| P2 `…vv5lm` | 节点有效性 `next(...)` 取**第一条**同 node_id 登记；合法轮换（旧 token inactive + 新 active）被判无效，`succeeded` 被改 `failed`、GET 403 | 「**存在**一条 active、同环境、同实际节点的登记即有效」；`actual == expected` 仍独立检查，**不靠禁止多条登记回避** | 1 条 |
| P1 `…vv5lr` | worker 未执行 `final_release` 要求的外部授权；节点跑完后订单转归他人，仍写 `completed` 并存会话答案（授权计数 3→3，它没问过） | worker 写结果前用**同一个**授权器再判一次归属（装配层注入，未注入即 fail closed）；归属丢失 / 授权源不可用都落 `failed`，不写正文、不写会话答案，轮次正常释放。**没有新造权限规则** | 归属丢失 / 授权源不可用 各 1 条 |

**真节点复跑**：用原样 `verify_chat_node_entry.py` 在修复后的树上重跑 —— **18/18 PASS**，failures=0。

**主线**：合入 `7d4ccb6`（#1051）。冲突只在 `deploy/m2-live-03/images.lock.json` 的
`source_sha256`：两边各自改过摘要，合并后两者都不成立，按合并后的树重算并 `--check`；
三个修复落进 `src/` 之后再重算一次（不重算会让
`test_gateway_context_digest_is_reproducible_and_matches_the_lock` 变红）。

**不接受为通用的东西**：模型 HTTP 请求数（正常 2、two-orders 4、round2-500 5、truncated 2）
是**这一个合成上游**的观测值，不是已证明的硬上限；后续真实供应商验收按实际请求计预算。
本片不开重试优化议题。
