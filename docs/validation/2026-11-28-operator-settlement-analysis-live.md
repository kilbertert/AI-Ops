# 管家端商城结算金额分析与 6 大清分规则核验上线部署及 41 端到端实跑验收回执（2026-11-28）

- **适用版本**：
  - 主功能合入：`origin/main` 修订 `34b1a4b454010059c2567eef6e6fac4f7cea1aac`（PR [#1839](https://github.com/kilbertert/AI-Ops/pull/1839)，[ADR-0374](../adr/0374-operator-mall-settlement-verification.md)，`v1.44.0`，标准 CD Run [`46153312191`](https://github.com/kilbertert/AI-Ops/actions/runs/46153312191)）；
  - 现场发现与补丁合入：`origin/main` 修订 `d7f6eb1f9537159ad02411c88c3611bbdc30488e`（PR [#1840](https://github.com/kilbertert/AI-Ops/pull/1840)，`v1.44.1`，标准 CD Run [`46155921096`](https://github.com/kilbertert/AI-Ops/actions/runs/46155921096)）。
- **适用环境**：生产环境 `41`（`aiops-41`，公网入口 `https://api.mall.qushiyun.com` 与 `https://ulinkmanage.h5.mall.qushiyun.com`，执行节点 `node-41`），同步核验独立客户环境 `jiejia` 无扰动。
- **只读与业务边界声明**：全程零修改公司侧仓库代码与业务库（`cloud_charging_pile`、`qumall_mall`）；Gateway 保持 `business_mutations: disabled`；本次验收证明自动化测试、生产只读跨库取数、Java 执行节点调用与公网双租户端到端链路成立，**`business_accuracy="unconfirmed"`**（尚未由业务/财务工程师逐笔签收业务准确率），且**不宣称银行已实际打款到账或点位奖已发放**。

## 一、已部署组件与 ADR-0188 上线顺序核对（`41` 宿主机）

| 顺序 / 组件 | 产物与版本标识 | 核对结果 |
|---|---|---|
| 0. 升级前一致性备份 | `/var/backups/aiops-41/pre-v1.44.0-20261128T140639Z`（`0700 root:root`） | 确认 0 个在飞 `agent_runs`（`issued`/`running`）且 30 分钟内无并发发布；备份 `gateway.db`（`.backup()`）、`chat_route_bindings.json`、`gateway-callback.conf`、`operator-tools.json`、`operator-exec-manifest.json`、容器列表及 Ulink/优链充两工作区 Dify 提供方/DSL/模型配置 |
| 1. 节点回调白名单 `aiops-node-callback.service` | `/etc/aiops-node-runtime/gateway-callback.conf`（SHA-256 `644d3e710bd0ebcc...`，保留宿主机 2 行 Lua 指令） | `/usr/bin/nginx -t` 通过，`active`；`POST .../tools/{cpms_order_settlement,cpms_order_settlement_check}/authorization` 与 `GET .../cpms/{mall-settlement,order-settlement-check}/ORDER-PROBE` 均放行直达 Gateway 并返回 `403` |
| 2. 执行节点 `node-41` 与清单模板 | `/etc/aiops-node-runtime/node/operator-tools.json`（SHA-256 `c6b6ec92707c308c...`，13 工具）与 `/etc/aiops-node-runtime/gateway/operator-exec-manifest.json`（`0600 aiops41:aiops41`） | 仅重建 `aiops-runtime-node` 容器（`ee4e8bac6e70`），其余 3 个容器 ID（`8fe2344efaa7` / `539aabd0a3b3` / `cb963492a046`）未变；`execution_node_capabilities` 实时上报 13 工具新 `tools_sha256` 并通过 `node_capabilities.require_compatible` |
| 3. Gateway `aiops-gateway-41.service` | 经标准 CD `46153312191` 上线 `1.44.0+34b1a4b45401`，复验修复后经标准 CD `46155921096` 滚动升级至 **`1.44.1+d7f6eb1f9537`**（同步发布 ACR 客户机镜像 `v1.44.1`） | `http://172.18.0.1:8788/health` 返回 `200`，`version=1.44.1+d7f6eb1f9537`，`business_mutations=disabled` |
| 4. Dify 提供方迁移与双租户受管发布 | Ulink（`1942105476598861824`，`137cc417-...`）发布至 **`v37` (`rev=2`)**（无损保留原 9 行运营商表达规范后缀）；优链充（`1941847786056323072`，`a000898b-...`）发布至 **`v3` (`rev=2`)** | 两工作区 `cpms-node` 提供方均完成 `migrate=True` 迁移，不可变快照冻结全部 13 项工具，审计账本留痕 |

## 二、首轮（`v1.44.0`）现场发现与 `v1.44.1` 闭环记录

在 `v1.44.0+34b1a4b45401` 首轮公网实跑中，Ulink（`qa_84adfeb8e2ed4412b3eff38f8e5be8d8` / `run_0b334KpTGOPNiMXzceFtIbncrg7bKz8LY2ZE33l5qPU`）与优链充（`qa_8add78d706af432a90c67d826b8ab4c9` / `run_NhdA2LIassOTzaHglQJhKg1pI4Ydh8ymY_0QgAw0Xfg`）完整跑通三工具链（`cpms_order_detail` → `cpms_order_settlement` → `cpms_order_settlement_check`），规则 3/4 通过，但规则 1/2/5/6 因 `shop_level` 查询返回空而安全触发「待验证」保护。

现场只读查证 `41` `qumall_mall.shop_level` 发现：`deleted_at` 为 BIGINT 时间戳，活跃未删除行的实际存储值为 **`0`**（而非 `NULL`）。随即通过 PR [#1840](https://github.com/kilbertert/AI-Ops/pull/1840)（`v1.44.1`）将条件修正为 `(deleted_at IS NULL OR deleted_at = 0)` 并补充 PyMySQL 真实返回形状（`deleted_at=0`、`config=None`、浮点费率 `0.3`）回归单测，经标准 CD `46155921096` 部署 `41` 后重跑双租户复验，全部 10 项核验细则均转为「通过」。

## 三、41 公网 HTTPS 端到端实跑验收结果（脱敏证据）

使用库外加密资产 `aiops-ulink-account-20261030`（Ulink）与 `aiops-youlianchong-account-20261030`（优链充）按公司管家原生 `tenant-app` 密码流程在内存换取短期个人 `Bearer` 令牌（零明文落盘/打印、零验证码绕过），携带 `X-Business-Entry: operator` 覆盖双公网域名：

| 用例 | 租户与入口 | 脱敏样本与追踪标识 | 实测结果与只读 SQL 逐分对照 | 判定 |
|---|---|---|---|---|
| 用例 1：Ulink 已支付结算单核验（`v1.44.1`） | Ulink · `https://api.mall.qushiyun.com` | 订单 `2126***9073` · `qa_b296641f15a34909b7eb81e72bc3e0c5` · `run_0I53VeiYqMVEI9r7kjdZxqOxdoqIr2UQ0M5JYJnxNNU`（33.9s） | `202 -> 200 completed`；`agent_runs`: `succeeded / matched / node-41 / 1.44.1+d7f6eb1f9537`；命中 `cpms_order_detail` + `cpms_order_settlement` + `cpms_order_settlement_check`；等级 `Normal`（整笔费率 3%），结算金额 `1.19`、实付款 `2.40`、通道费 `0.02`、平台服务费 `0.07`、分佣基数 `1.12`，**6 大规则 10/10 项全部「通过」（差额 `0.00`）**；保留 Ulink 定制格式且附带不证明实际打款到账声明 | 通过 |
| 用例 2：优链充已支付结算单核验（`v1.44.1`） | 优链充 · `https://ulinkmanage.h5.mall.qushiyun.com` | 订单 `2126***5521` · `qa_fb4dd469f2264c8fa34cf33160efa314` · `run_Xxz6WO59ZRjuU-wuYU75kFdhYXlg3hwffd4LvJVTHt4`（34.0s） | `202 -> 200 completed`；`agent_runs`: `succeeded / matched / node-41 / 1.44.1+d7f6eb1f9537`；命中两新工具；等级 `分佣0.03`（整笔费率 0.3%），结算金额 `2.24`、实付款 `4.50`、通道费 `0.02`、平台服务费 `0.01`、分佣基数 `2.23`，**10/10 项全部「通过」（差额 `0.00`）**；附带只读及非打款到账限制说明 | 通过 |
| 用例 3：0 元全额优惠边界单（`v1.44.1`） | 优链充 · `https://api.mall.qushiyun.com` | 订单 `2122***1905` · `qa_bc935b4f82dc45ae8490458310bf7b28` · `run_0yJQI2e8eainkuXro8ISi4iJWN794H_jI5fWjXvFXeM`（31.3s） | `202 -> 200 completed`；如实解释商城结算单已生成但因 5.69 元全额店铺优惠抵扣致实付 `0.00`、结算/服务费/分佣基数均为 `0.00`，**10/10 项自洽「通过」**，无 500 或误报 | 通过 |
| 用例 4a：TTL 内跨租户 `qa_id` 回读隔离 | 优链充读 Ulink `qa_id` · `https://api.mall.qushiyun.com` | 分别在 `v1.44.0`（`qa_84ad...`）与 `v1.44.1`（`qa_b296...`）3 分钟 TTL 内紧接实测 | 本人 `GET` 返回 `200`；跨租户 `GET` 返回 **`404 QA_NOT_FOUND`** | 通过 |
| 用例 4b：跨租户订单越权查询拦截 | 优链充查 Ulink 订单 · `https://api.mall.qushiyun.com` | `qa_7812511b83f34aa3b52159158eab06ba` · `run_MyRAa5QFLgYl_YM30sjxZgbW4wrmfNwNErFmFuSiNcw`（10.5s） | 订单详情阶段拦截拒绝，`tool_summaries=None`，零跨租户订单或商城结算数据泄露 | 通过 |
| 用例 4c：匿名请求鉴权拦截 | 无 `Authorization` · `https://api.mall.qushiyun.com` | `POST /v1/assistant/questions` | 返回 **`401 ACCESS_TOKEN_REQUIRED`** | 通过 |
| 用例 5：既有非结算充电诊断无回归 | Ulink · `https://api.mall.qushiyun.com` | 订单 `2126***9073` · `qa_899602daf99f4a68b0f35239802180b3` · `run_YFDvJleMk2QYZkGqgfT2bEEAu8Yi5gwb_SOlHlcr2aE`（51.8s） | `202 -> 200 completed`；仅按需调用 `cpms_order_detail` 与 `cpms_charging_timeline`，未误调结算工具 | 通过 |
| 用例 6：独立客户环境 `jiejia` 与路由稳定 | `41` 节点服务 + `https://api.juxichongdian.cn` | `aiops-environment-node@jiejia.service` + 公网匿名探活 + `chat_route_bindings` | `aiops-environment-node@jiejia.service=active`，`jiejia` 公网 `POST /v1/assistant/questions` 正常返回 `401 ACCESS_TOKEN_REQUIRED`；`41` 两现役管家路由稳定维持 `v37` / `v3`（`rev=2`） | 通过 |

## 四、多环境部署矩阵与后续客户环境 Day-2 升级清单

| 环境 | 当前状态 | 本次动作与后续升级说明 |
|---|---|---|
| `41`（Ulink / 优链充控制面与生产网关） | **已上线 `1.44.1+d7f6eb1f9537` 并通过双公网实跑验收** | 已完成回调白名单、节点工具声明、Gateway、Dify 双租户发布（`v37` / `v3`）及全链路复验 |
| `36`（Dify 宿主、`aiops-dify-edge`、`dify-web`、`kb-service`） | 无需变更 | `dify-web` 日志组件对新增工具名自动原样展示，其余组件不感知 CPMS 工具清单 |
| `jiejia`（客户机 `jiejia-gw` + `node-jiejia`） | 保持既有冻结快照稳定运行（未扰动） | `v1.44.1` ACR 客户机镜像已随 CD `46155921096` 自动推送；后续按需启用时依次执行：(1) `41` 更新 `/etc/aiops-environment-nodes/jiejia/{operator-tools.json,callback.conf}` 并重启 `aiops-environment-node@jiejia`；(2) 更新 `jiejia` Nginx `/v1/internal/` 工具片段并 `nginx -t && reload`；(3) 在 `/aiops/environments` 将 `jiejia-gw` 经 ACR OCI 无损升级至 `v1.44.1`；(4) 登录 `jiejia` 工作区迁移 `cpms-node`、启用两新工具并热发布 |
| `95` / `124` | 目录中当前 `enabled: false`（未启用） | 未来经 `/aiops/environments` 五步向导开通冷装时，`onboarding/runtime.py` 自动派生完整 13 工具与最新 SOP |

## 五、未宣称范围

- 尚未覆盖现网真实「模式 1 全额跟随」「模式 2 电费跟随服务费」「规则 2 启动费通道费溢出未扣减」「停车费/占枪费/退款/按度定额抽成」订单的线上实单抽样（上述分支已由 `tests/operations/test_order_settlement.py` 24 条自动化测试验证）。
- `business_accuracy="unconfirmed"`：本回执不替代业务/财务工程师的人工准确率签收，也不证明银行实际打款到账。
