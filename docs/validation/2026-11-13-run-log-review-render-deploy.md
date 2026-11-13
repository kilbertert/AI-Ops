# 复核行渲染修正上线 + 页面实测验收（本轮补齐视觉验收缺口）

- 日期：2026-11-13
- 合并：`51a18b86`（PR #1498 squash）
- 镜像：`aiops/dify-web-patched:review-render-51a18b86`

## 36 Dify Web（手工换镜像）

- 本机 webpack 构建，上游 sha 核对通过；容器内编译产物含
  `事实复核观察`/`已进行` 新文案（server page.js + static chunk）。
- `docker save | ssh yidong-36 docker load`；`/opt/dify/deploy/.env` 备份
  （`.env.bak-web-render-*`）后改 `DIFY_WEB_IMAGE`；
  `docker compose up -d --force-recreate web` 重建 `deploy-web-1`。
- 运行容器内复验产物含新文案；入口 `http://127.0.0.1:10008/` → 307。

## 真实页面验收（本轮新做，上一轮缺口）

经 `dify.ranlei.work` 以真实 ulink 账号登录控制台（公司账号+验证码通道，
云桌面浏览器），打开管家端 app 日志页、点开真实运行记录：

- 运行 `2026-11-12T13:53:02`（订单 2116050516255272961，成功 14.93s）详情：
  - `事实复核… · 已进行 12.65 秒`——复核开始事件正确渲染为进行中，
    **修复前同一位置实测为「事实复核：通过 · 耗时 未采集」**（旧镜像下
    同一条 run 先后两次取证对照）。
  - `事实复核：复核通过 · 耗时 1.53 秒`——完成事件不变。
  - `调用 订单详情… · 已进行 3.13 秒`（running 工具行）、
    `第 1 次调用 订单详情：成功 · 耗时 0.35 秒 · 累计 3.42 秒` 与
    `入参=… · 应付=7.84 · 实付=8.0 · 退款=2.0 · 电量=1.7111` 摘要行正常。
- 证据截图：`/home/claude/.cache/aiops-evidence-20261113/run-detail-review-render-fixed.jpg`
  （含真实订单号，不入库）。

## 顺带核清的三个「看起来怪」现象

- 问题列 `REDACTED`：`gateway_store.py` 在**写入** `assistant_questions.question`
  时即 `redact_text`（不 preserve 订单号）——存储侧脱敏，非页面缺陷；
  订单号在专属列照常投影。
- 「业务结果已过期」：答复按保留策略过期，日志页只保留运行账本，设计如此。
- 复核两行 = 开始+结束各一事件，与工具调用同款；修的是第一行文案。

## 验收边界

- 本轮实测 allow/through 路径的渲染；「事实复核观察：xxx」非 allow 行与
  refused 行只有 stub 用例与 esbuild 解析证据——生产出现非 allow verdict
  时再补真实截图。
- 页面排版/信息密度本身的「简陋感」未动：本轮只修错误渲染与口径，
  未改布局。
