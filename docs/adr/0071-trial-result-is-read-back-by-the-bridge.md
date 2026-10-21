# 71. 试运行结果由桥读回：终态只有一个权威，前端不抄词表

日期：2026-10-21
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_console_publish.py`（`read_trial`、`TrialReport`、
`POST /__aiops/trial/result`）、`ops/dify-console-bridge/dify-publish-button.js`、
`ops/dify-console-bridge/dify-console-publish.conf`

## 背景

「在 AI-Ops 试运行」那颗按钮上线后只报一句话：`已提交试运行（tr_…），状态：queued`。
结果留在网关的 `standard_diagnoses` 里，运营手上没有任何东西能把它读回来 ——
**提交成功不等于看得到答案**。而这条链的失败曾经被报成"没登记绑定"，运营会去查绑定表
（见 ADR-0064），所以"结果到底是什么"这件事必须有一个**运营够得着**的出口。

约束有三条，都不是可选的：

1. 读的是一次**真订单**上的诊断结果，凭据不能比提交那条更弱；
2. 终态词表已经存在（`async_job_lifecycle.DIAGNOSIS.terminal`：`completed` /
   `inconclusive` / `failed` / `cancelled` / `expired`），前端不能有第二份；
3. 页面可能开一整天，轮询必须有上限并能在关闭时停止。

## 决策

**桥新增一条读回路由，终态由网关判，前端只呈现。**

1. `POST /__aiops/trial/result`，body 只许 `{"trial_id"}`。桥拿**同一把试运行钥匙**与
   **同一组发起人头**（抽成 `_initiator_headers_for`，提交与读取共用）去打
   `GET /v1/dify/trial/<id>`。
2. 桥返回 `{read, reason, status, terminal}`；`terminal` 由
   `TRIAL_TERMINAL_STATUSES = DIAGNOSIS.terminal` 判 —— 词表只有这一处。
3. 前端面板按 `terminal` 停轮询，最多 150 次 × 2s，关闭即停；报告文字由桥拼
   （`summary` + `root_cause` + 证据条数 + 未取到的数据源）。
4. nginx snippet 增加一条**精确匹配**的 `location = /__aiops/trial/result`，与提交那条
   转发同一组头；静态判据断言"脚本打的每条路径都有 location"。

## 被否掉的替代形态

- **让浏览器直接打网关的 `GET /v1/dify/trial/<id>`。** 它需要试运行钥匙与发起人头，
  两样都在服务端；交给浏览器等于把"能在真数据上跑/读诊断"的凭据公开。否掉。
- **做成 GET `/__aiops/trial/<id>`（更 RESTful）。** 浏览器的同源 GET **不发
  `Origin`**，要保留同一条门就得为它发明第二套判据（例如只靠 Referer + CSRF）——
  一套更弱、且与提交不同的门。**否掉**，代价是这条路由读起来不像 GET。
- **前端自己判终态**（`status === 'completed'` 才停）。看起来更简单，但那是第二份词表：
  网关将来把 `inconclusive` 也算成功时，面板会永远转圈；反过来若前端提前认定，
  它会在作业还在跑时停止刷新。**否掉。**
- **不加轮询上限，靠作业自己的 TTL 收敛。** 页面开着就一直在发请求；而"作业已过期"
  这件事本身也是要靠轮询才能发现的。上限与 TTL 是两件事。**否掉。**
- **把完整报告塞进面板**（全部 hypotheses / next_steps）。面板的价值是"有没有结果、
  是什么"，完整结构仍在那条 JSON 路由上；把报告全文铺在浮层里既读不完也挡视线。

## 后果

- **桥多了一个会读生产诊断内容的路由。** 它的爆炸半径等于"这次试运行读得到的那些字段"，
  且需要同一把钥匙 + 同一个发起人范围；未登记绑定的 app 借不到（Referer 门仍在）。
- **前端多了约 120 行 DOM 与轮询代码**，没有构建、没有框架，靠静态判据钉住形状
  （`tests/test_dify_publish_button.py`）。这是注入式资产的固有代价，与 ADR-0021 同源。
- **面板不替代 `GET /v1/dify/trial/<id>`**：手上有 `trial_id` 的人仍应读那条路由拿完整
  报告；面板只是"运营不敲命令也能看见"的最低限度出口。
- 轮询上限（5 分钟）与诊断 TTL（15 分钟）不等：超时后面板说"已停止自动刷新"，作业
  仍在网关里继续，重新点一次会读到它。
