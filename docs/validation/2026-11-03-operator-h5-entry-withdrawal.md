# 撤回管家 H5 首页「智能检测」入口：合入、部署与真实页面验收

执行时间 2026-11-03（UTC 06:4x–07:1x，北京时间 14:4x–15:1x）。环境：41 生产、`ulinkmanage.h5.mall.qushiyun.com`。

## 一、撤的是什么

**只撤首页那一行入口按钮，不是撤聊天页。**

「智能检测」聊天页本身是公司 2026-09-30 `e91d7bbc`「管家端集成AI客服」引入的，不是本轮交付。
我们加的是 `pages/home/index.vue` 里的入口按钮（`57b4018e`），以及同轮的发送/停止按钮、
生产环境关闭诊断模拟、请求不再打印含授权头的 cURL 三处行为修复。

| 文件 | 处置 |
|---|---|
| `pages/home/index.vue` | **删除**首页导航栏 `<button data-url="/aiPackage/pages/chat/chat">` |
| `aiPackage/pages/chat/chat.vue` | 保留（聊天页与行为修复不动） |
| `aiPackage/api/aiops.js` | 保留 |
| `locale/{zh,en}.json` `chat.input.*` | 保留 |
| `tools/build-h5.py`、`tools/h5/`、`tools/verify-h5.py` | 保留 |

撤掉入口后，聊天页仍由 `pages.json` 注册在 `/aiPackage/pages/chat/chat`，**有 URL 就能直达**。

## 二、交付路径

- 分支 `fix/remove-operator-smart-diagnosis-entry` 基于公司远端最新 `ac9624f2`。
- MR **!146**：<https://git.qushiyun.com:801/s2b2c-front/qumall-app/-/merge_requests/146>，已合并为
  `bb5529829dd03554d162b8e1bbfdc9beac1df4ca`，无冲突。
- 公司仓归类为 externally governed（`git config serverpolicy.repositoryclass=external`），
  按公司 MR 流程交付；合并方式与上一轮 MR !145 一致。

## 三、制品与安装

- 从**干净合并修订** `bb552982` 完整构建 594 文件，`build-manifest.json` 记录 `dirty=false`
  与同一 `source_commit`；构建脚本为仓内 `tools/build-h5.py`（服务端不执行未声明代码）。
- 制品 tar 与部署工具经 `dev-host cp --artifact-sha256` 传到 41，远端逐件复核摘要一致。
- `deploy/operator-h5.py` 安装：先校验清单（脏修订/文件集合/逐文件 SHA-256），备份现有
  vhost、extension、proxy 到 `/root/h5-backup-bb552982`，再切静态根、`nginx -t`、reload。

切换后的配置差异**只有一行**：

```diff
-    root /www/wwwroot/ulinkmanage-h5-5121d9f8;
+    root /www/wwwroot/ulinkmanage-h5-bb552982;
```

extension 与 `operator-h5-proxy.inc` 无差异。旧静态根 `/www/wwwroot/ulinkmanage-h5-5121d9f8`（36M）保留。

## 四、真实页面验收（原管家 H5，非注入）

用加密清单账号 `aiops-ulink-account-20261030`（`ulink` 租户 `1942105476598861824`）在
`pages/login/login` **正常输入账号口令登录**，验证码由页面自身处理；`POST /upms/token/login` 200，
`localStorage.CLOUD_ACCESS_TOKEN` 写入，跳转首页。

浏览器为验收机 `windows-rl1` 上的 Playwright/Chromium（该机到公网入口可达；开发机不可达）。

### 4.1 首页无入口

登录后首页正文（逐字节选）：

```
你好，ulink
累计总收益(USD) 0
场站营运数据 11/03 14:59
桩枪监控 121/288 在线/离线（桩）
```

**页面源码中 `智能检测` / `Smart Diagnosis` / `智慧檢測` / `Diagnóstico` 均无匹配**
（`home_has_biaoji=false`）。首页加载的首页 chunk 为 `pages-home-index.e0dc73ac.js`：
`size:"mini"=0`、`aiPackage/pages/chat/chat=0`、`chat.diagnosis.title=0`。
旧根同名 chunk `c6bfb8d5` 三项分别为 `1/1/1` —— 这是"按钮确实没了"的字节级对照。

### 4.2 聊天页仍可用

直接导航 `https://ulinkmanage.h5.mall.qushiyun.com/aiPackage/pages/chat/chat`，页面渲染正常，
`.mode-btn` 切换键盘后输入可见、`.send-btn` 可点（44×40 px）。

提问「我有哪些站点」，网络记录：

```
GET  /v1/faq/recommendations                    200
GET  /v1/shortcuts                              200
POST /v1/assistant/questions                   202  qa_873a08010a9f4b70b1516d794d9e5f46
GET  /v1/assistant/questions/qa_873a0801…      200  （轮询若干次）
```

页面正文（节选，逐字）：

```text
我帮你查了一下，当前账号可见的结果已列在下方。如果需要按站名或地点进一步筛选，告诉我关键词就行。

当前账号可见范围内共 161 个站点，这里显示前 10 个：
1. ZDH--Test2026-11-03 03:00:10 — 984HF52X+QW
5. بغداد مول (Baghdad Mall) — Baghdad, Baghdad Governorate, Iraq
…
查询达到显示或分页上限，结果可能不完整，请缩小地点范围。
```

服务端（只读库 `gateway.db`，按 `qa_id` 取，不取最新行）：

| 项 | 值 |
|---|---|
| `assistant_questions.status` / `error_code` | `completed` / 空 |
| `tenant_id` | `1942105476598861824` |
| `agent_runs.state` / `exec_config_state` | `succeeded` / `matched` |
| `chat_binding` | `operator_order_diagnosis:production-41:1942105476598861824` |
| `route_revision` / `operation` | 17 / `operator_conversation` |
| `scope.caller` | `username=ulink`、`user_type=1`、`effective_tenant_id` 同租户 |

即请求命中的是该租户的 **operator 绑定**，不是消费者绑定，也不是未登记卡片。

## 五、OH-04 单订单验收（补做）

验收计划 OH-04 要求「选单 + 同次 Java 与两项 CPMS 证据」。用真实订单跑一次：

- 页面输入「帮我分析这笔单的电费 `2117490429826162689`」（ulink 租户一张真实订单），
  `POST /v1/assistant/questions` → 202，轮询到 `completed`。
- 页面正文可读：订单号、状态「充电结束」、预付费 10.0、电费 1.11、服务费 1.11、启动费用 0.5、
  合计 2.72、系统记录支付金额 2.72、退款 7.28、电量 0.011 kWh、充电时长 6、停止原因 `Remote`、
  **本次取得监测样本：0**，并明确写出「账务记录不证明银行到账，当前状态不证明历史故障原因」。
  数据来源段列出「订单详情」与「充电监测样本」两条及各自 UTC 时间。

服务端（`agent_runs`，按 `qa_id` 取）：

| 项 | 值 |
|---|---|
| `qa_14ec28a4…` `assistant_questions.status` | `completed`，`error_code` 空 |
| `run_id` | `run_-a5DLdlNBioeTCui5k_SqOZEK_PrUfDyiMbfwpaDn4k` |
| `order_no` | `2117490429826162689`（与问句逐字一致） |
| `operation` / `chat_binding` | `operator_order_diagnosis` / `operator_order_diagnosis:production-41:1942105476598861824` |
| `state` / `exec_config_state` / `route_revision` | `succeeded` / `matched` / 17 |
| `actual_node_id` | `node-41` |
| 工具 1 | `cpms_order_detail` → 真实字段（`electricityFee=1.11`、`launchFee=0.5`、`electricity=0.011`、`gunPower=120`） |
| 工具 2 | `cpms_charging_samples` → `absence=no_samples`、`total=0`、`truncated=false` |

即 Java 节点确实取了两项 CPMS 数据，**监测为空时页面写明缺证、没有推断设备正常**。

## 六、同域路由白名单复核（安装后）

`ulinkmanage` vhost 的 `aiops-assistant.conf` 与 `operator-h5-proxy.inc` 与备份逐字相同（无差异），
线上实测：

| 请求 | 结果 |
|---|---|
| GET `/v1/faq/recommendations`、`/v1/faq/catalog`、`/v1/shortcuts`、`/v1/assistant/questions/qa_<32hex>` | 401（已转发、缺身份） |
| POST `/v1/faq/answer`、`/v1/assistant/questions`、`…/cancel` | 401（已转发、缺身份） |
| POST `/v1/faq/catalog`（未声明的 POST） | 405 |
| GET `/v1/agents`（未声明的管理路径） | 200 `text/html` = SPA 回退，未进上游 |
| GET `/v1/media/abc`、`/v1/health-report-jobs/job_x`、`/v1/standard/diagnoses/d_x` | 200 = SPA 回退，未进上游 |

机器人扫描（`/.bash_history`、`/secrets.json` 等）同样落 SPA 回退，未触及管理上游。

本仓 `tests/test_h5_assistant_routes.py` 3 项通过（nginx 片段与 `ops/nginx/h5-assistant-routes.txt` 一致）。

## 七、回滚

```bash
python3 deploy/operator-h5.py --restore --backup /root/h5-backup-bb552982 --nginx /usr/bin/nginx
```

恢复后 vhost 指回 `ulinkmanage-h5-5121d9f8`，即上一轮已验收的制品；新制品目录不删除以便取证。

## 八、边界

- **只验证了首页无入口、聊天页可直达、一次真实提问全链路**。未覆盖：多语言界面、原生 APK/IPA、
  预约/计费/退款等业务写路径。
- **OH-04 已按单订单补做并通过**（§五）。站点查询那次（`operator_conversation`）不构成 OH-04 证据，
  两者是不同的路由与工具集合，不能互相顶替。
- 公司仓该分支没有流水线，构建与浏览器检查均在本地/验收机执行，**不代表公司 CI**。
- 撤回入口后，产品侧若仍需该能力，应改为在明确的位置（而非首页导航栏）提供入口，
  由调用方导航到 `/aiPackage/pages/chat/chat`。
