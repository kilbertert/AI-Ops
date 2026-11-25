# 双环境（41 / 95）P5 正式 HTTPS 联合验收与一键开通现场回执（2026-11-25）

- **适用版本**：
  - 41 控制面 Gateway：`1.40.4+6054bc880093`（本轮依次合入并生效 PR #1724 `3aead1b4`、PR #1727 `a9889801`、PR #1728 `2a7ac4d4`、PR #1733 `7150e0ce`；41 切后消费端复验基线 `1.40.4+46c200d43ebb`；`/health` 返回 `business_mutations: disabled`）。
  - 95 目标面 Gateway：通过页面驱动的一键开通受管安装器，由 `1.40.4+fc9594a9a50b`（`gateway.db` `PRAGMA user_version=2`）无中断前向迁移升级至 **`1.40.4+a98898014689`**（`PRAGMA user_version=4`，含 `async_jobs` 表迁移与演练备份；`/health` 返回 `business_mutations: disabled`），节点 `node-95`。
  - 36 共享控制台：`d871c40c` 配套（`kb-service --scoped-account`、`aiops-dify-login-proxy` 多环境 `ADR-0276` 启用、`aiops-dify-console-bridge`、`deploy-web-1`）。
- **适用范围**：41 唯一目录切换后双租户消费端实网复验、36 多环境登录切流核验、95 跨 schema 原地升级，以及升级后 95 双真实租户（平高 `2019588094906601472`、小趋充电 `1783022023241633792`）经 `/aiops/environments` 页面一键开通/验证/正式发布/正式 HTTPS 三业务 + 双端同会话追问 + 双向跨租户互查 + 双环境同窗并存 + 受管恢复停用闭环。
- **未触碰边界**：零公司仓库改动、零公司服务/业务库写操作、`124` 保持 `draft_only` 未动；验证完毕后 95 已按受管流程恢复至双租户停用安全态（`95` 本地 `environment.json revision: 14`，`enabled=false`，双租户 `false`，`active_routes=0`；41 中央 `registry.json revision: 13`，`41` 两现役租户 `true`，`95` 双租户 `false`）。

---

## 一、41 切后双租户复验与 36 多环境登录切流

1. **41 切后双租户正式 HTTPS 复验（`1.40.4+46c200d43ebb`，`execution_environment=41`）**：
   - Ulink（`1942105476598861824`）与优链充（`1941847786056323072`）通过正式入口完成知识问答（命中 36 重键后新作用域 KB 命名空间，各 6 blocks）、本人订单状态查询、同会话追问与跨租户订单互查拒绝（`404 ORDER_NOT_FOUND`）；`node-41` 实时心跳在线（11 工具）。
2. **36 多环境登录切流（`ADR-0276`）**：
   - 真实人工通过图形验证码完成 `https://dify.ranlei.work/signin` 登录并进入 `/aiops/environments` 管理页；确认 `GET /__aiops/login/environments 200` → `POST /__aiops/login 200` → `POST /__aiops/publish/check 200` → `POST /__aiops/manage 200` 全链路正常。

---

## 二、全链真机实测捕获并闭环的 4 项工程缺陷与 1 项观察

| 编号 / PR | 现场暴露现象 | 修复与真机复验结果 |
|---|---|---|
| **PR #1724**（`3aead1b4`，ADR-0320） | 95 平高首次激活窗口内，正式 HTTPS 管家入口返回 `403 OPERATOR_SITE_SCOPE_REQUIRED`（公司 UPMS `/user/ds` 返回系统错误后降级推导遗漏了租户顶层账号 `type ∈ {-1, 1}` 归一为 `SCOPE_TYPE_ALL`） | 对齐 `EntryCallerResolver._normalize_operator_scope` 与既有 `data_scope_rule.py` 规则；升级 95 后平高与小趋正式 HTTPS 管家 `order`/`offline` 及追问均 `200 completed` |
| **PR #1727**（`a9889801`，ADR-0272） | 95 从 `fc9594a9`（schema `v2`）升级至 `v3/v4` 时，受管安装器 `runtime_check` 严格拦截并触发自动回滚（顺带实证 P3 升级失败自动回退与原服务零损坏） | 安装器补齐停服前隔离副本演练、停服后 `0600` 校验备份、单事务前向迁移与失败原库恢复；95 实机一次无中断完成 `gateway.db user_version 2 → 4` 迁移 |
| **PR #1728**（`2a7ac4d4`，ADR-0329） | 离线应用包增至 24.9 MB 后跨太平洋链路单轮上传触顶 300s 且 `reconcile` 重复重下重传 | 前置 `guard_staged_archive` 按 root/`0600`/同 SHA-256 跳过重传并调优传输预算至 `180s/480s/600s`；95 升级及后续小趋一键开通均稳定通过 |
| **PR #1733**（`7150e0ce`） | `/aiops/environments` 5 秒轮询 `tasks.inspect()` 在独占锁内重复解压 24.9 MB 归档耗时 ~7.4s，导致客服验证回调撞穿 30s 超时 | `tasks.inspect()` 锁内去重 + 按实时校验摘要缓存清单解析；41 实测 20 任务 `inspect()` 从 **7.4s 降至 12–15ms**（提速约 600 倍），随后平高与小趋三能力验证均一次通过 |
| **待后续跟进观察** | 多次 `publish → restore` 反复切回同一源配置摘要时，`source_configuration(restore=True)` 的 `O_EXCL` 备份会撞上历史同名 `references.json.<sha>.bak`；同时 `publication.py` 终态判定将 `phase=="restored"` 排在 `state=="reconcile"` 之前，令平高首次恢复在 95 已停用后提前标 `cancelled` 而跳过中央目录同步 | 归档冲突 `.bak` 并重跑受管恢复后 41 中央目录干净推进至 `rev 11`；小趋恢复前提前归档冲突 `.bak`，首次恢复即无报错收敛（`error_code=None`）。后续可单开修复 PR 调整终态判定顺序并避免同名备份冲突 |

---

## 三、升级后 95 双租户正式 HTTPS 全量验收与双环境并存结果

### 1. 第一租户：平高（`2019588094906601472`）

- **激活与版本**：页面驱动 `candidate-validate`（`task_895677f3...`）三能力全绿 → `environment-publish`（`task_66cca367...`）于 `2026-11-25T17:19:38Z` 激活（`41 reg rev 10` / `95 reg rev 11`，`1.40.4+a98898014689`，`db_user_version=4`）。
- **正式 HTTPS 三业务 + 双追问 + 匿名负向（`4/4` 通过）**：
  - `consumer`（`qa_bf9946e3869441ebb4ea4b4953c0f841`，`7.62s`）：`chat_dify_runs` 核实 `state=succeeded`，实际调用 `status`（命中本人订单）+ `knowledge`（`found`）。
  - `consumer_followup`（`qa_b953dd3a12a24ca9a307c18a1db95653`，`4.44s`）：同会话追问沿用同一订单（`status=found`）。
  - `order`（`qa_c02aabe5ce474fd887b63d653249d7e2`，`53.05s`，`node-95`）：`agent_runs` 核实 `state=succeeded`，实际调用 9 项 CPMS 工具（`cpms_order_detail`、`cpms_charging_timeline`、`cpms_charging_samples`、`cpms_order_tariff`、`cpms_order_billing`、`cpms_order_amount_check`、`cpms_order_process_stats`、`cpms_device_offline`、`cpms_order_communications`）。
  - `operator_followup`（`qa_c8075f56a9ca4380acd57c299dbc77c4`）：同会话追问沿用同一诊断订单完成回复。
  - `offline`（`qa_01dfe05cefee4d0594d0dbb37f9596fc`，`11.10s`，`node-95`）：`cpms_device_offline` 命中目标设备（`new_events: 3`）。
  - `anonymous_refused`：`HTTP 401`。
- **跨租户互查隔离（平高 → 小趋，3/3 零泄露）**：
  - 消费单（`qa_453116e7ecc34546a77a0fe16216c471`）：`ORDER_NOT_VISIBLE`；
  - 诊断单（`qa_b6bc5876177a4ee9a8c97af2188f9a94`）：`tool_summaries` 为空，未返回对方单据事实；
  - 设备离线（`qa_c6ab9222f5c1420d9b06a0de5b27143f`）：工具拒绝 `OPERATOR_OFFLINE_UNAVAILABLE`。
- **双环境同窗并存与单租户粒度准入**：
  - 同期 41 Ulink 正式 HTTPS 问答 `200 completed`（`qa_482a23f9808e4856aaba30bffe893401`，6 blocks）；
  - 同期 95 未开通小趋租户消费端与管家端均被精确拒收 `403 INSUFFICIENT_SCOPE`。
- **受管恢复停用与停用后拒收**：
  - 页面触发「恢复本次开通」后 95 本地目录推进至 `rev 12`（`enabled=false`，双租户 `false`，`active_routes=0`），41 中央目录推进至 `rev 11`（双租户 `false`）；
  - 恢复后复测平高消费端与管家端正式 HTTPS 请求均返回 `403 INSUFFICIENT_SCOPE`（`2/2` 通过）。

### 2. 第二租户：小趋充电（`1783022023241633792`）

- **页面一键开通与发布激活**：通过真实浏览器在 `/aiops/environments` 触发「一键开通：小趋充电（`1783022023241633792`）」（`task_1c27ef1150a5d841c66008b5f4a6755a`），4 阶段一次性全绿收敛 `ready_to_publish=true`（`candidate-prepare` → `candidate-install` `1.40.4+a98898014689` → `candidate-register` → `candidate-validate` 三能力全 `passed`，含同摘要自动续签计划 `plan_f4bb74ce...`）；随后点击「确认发布并开通」（`task_23f2c299e03bc899a03eed44ac24f2d6`）于 `2026-11-25T18:37Z` 激活（`41 reg rev 12` / `95 reg rev 13`，`routes: {operator: 3, consumer: 3}`）。
- **正式 HTTPS 三业务 + 双追问 + 匿名负向（`4/4` 通过）**：
  - `consumer`（`qa_e8292688c9d841f18d0e1d99d7cf4e18`，`6.68s`）：`chat_dify_runs` 核实 `state=succeeded`，实际调用 `status`（`found`，命中本人订单）+ `knowledge`（`found`）。
  - `consumer_followup`（`qa_53fae960bf8f4b90b4ec9e63cdb1a470`，`3.96s`）：同会话追问沿用同一订单（`status=found`）。
  - `order`（`qa_db1a5630af4c4144a009ceeb58fe1e84`，`40.64s`，`node-95`）：`agent_runs` 核实 `state=succeeded`，实际调用 8 项 CPMS 工具（`cpms_order_detail`、`cpms_charging_timeline`、`cpms_charging_samples`、`cpms_order_tariff`、`cpms_order_billing`、`cpms_order_amount_check`、`cpms_order_process_stats`、`cpms_order_communications`）。
  - `operator_followup`（`qa_1fc6e6e2fb4a4dd5ac63e2ebed08bedb`，`10.98s`，`node-95`）：同会话追问沿用同一诊断订单（`cpms_order_detail`）。
  - `offline`（`qa_2f1e4304e2ca4111a209a131b6831158`，`19.33s`，`node-95`）：`cpms_device_offline` 命中目标设备（`new_events: 2`）。
  - `anonymous_refused`：`HTTP 401`。
- **跨租户互查隔离（小趋 → 平高，3/3 零泄露）**：
  - 消费单（`qa_a20f6775e2194ff29cfa4d7e44ee99a5`）：`chat_dify_runs` 工具 `status` 返回 `refused`；
  - 诊断单（`qa_34e78d3f31774ef781f4b3ef0b0e4b73`）：`agent_runs` `tool_summaries` 为空，未返回对方单据事实；
  - 设备离线（`qa_346edc7956a94db09262fe7c5520f3ea`）：`cpms_device_offline` 明确拒绝 `OPERATOR_OFFLINE_UNAVAILABLE`。
- **双环境同窗并存与单租户粒度准入**：
  - 同期 41 Ulink 正式 HTTPS 问答 `200 completed`（`qa_64f8897164c6480abdc83ff6288d481d`，6 blocks）；
  - 同期 95 已停用平高租户消费端与管家端均被精确拒收 `403 INSUFFICIENT_SCOPE`。
- **受管恢复停用与停用后拒收**：
  - 页面触发「恢复本次开通」后一次无报错收敛（`state=cancelled`、`phase=restored`、`activated=false`、`error_code=None`），41 中央目录推进至 `rev 13`（95 双租户均 `false`、`references.json` 还原），95 本地目录推进至 `rev 14`（`enabled=false`，双租户 `false`，`active_routes=0`）；
  - 恢复后复测小趋消费端与管家端正式 HTTPS 请求均返回 `403 INSUFFICIENT_SCOPE`（`2/2` 通过）。

---

## 四、明确未宣称范围与外部阻塞边界

1. **业务语义准确率未人工验收**：本回执证明了跨环境架构、唯一目录、页面一键开通、升级迁移、正式 HTTPS 链路、工具/对象/窗口落库、双向跨租户隔离与受管恢复停用的技术闭环，**不宣称业务准确率已获工程师或业务方签收**。
2. **原生 Android 客户端外部阻塞**：`#1353`（连续会话）与 `#1422`（Markdown 呈现）仍待公司发包，H5 / 正式 HTTPS API 证据不替代原生 APK 验收。
3. **同租户跨用户隔离**：尚缺第二真实用户授权材料，未创建或冒充公司用户。
4. **95 历史误回显测试 operator 令牌撤销回执**：此前 95 现场实证公司 UPMS 不依赖 JWT `exp` 自然失效，后端精确撤销回执仍未取得；本轮受控验收窗口经用户明确放行用于验证并已在结束后完整恢复至 95 双租户停用安全态（`enabled=false`、`active_routes=0`）。
5. **管理页视觉与交互体验改造**：`/aiops/environments` 当前已覆盖四区总览与一键开通功能闭环，其界面美观与交互流畅度重构已列为下一阶段优先需求，不在本回执宣称范围内。

> 原始脱敏外私有运行证据（`0600` 权限，含请求/响应结构与 95 `gateway.db` 逐腿核验摘要）仅留存于受控私有运维缓存目录，不入公开仓库。
