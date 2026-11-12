# 答复复核降为观察（ADR-0252）双端上线记录

- 日期：2026-11-12
- 合并：`e9827d35`（PR #1494 主补丁）+ `fe4fd86a`（PR #1495 Devin 跟进补齐）

## 41 Gateway（CD 自动部署）

- 主补丁 CD run `43221145651` success；跟进 CD run `43223832344` success。
- `GET /health`：`{"ok":true,"version":"1.9.1+fe4fd86ad419",...}`——版本即合并提交，
  `aiops-gateway-41.service` 21:06 CST 重启在册。

## 36 Dify Web（手工换镜像）

- 本机 webpack 构建 `aiops/dify-web-patched:review-observe-fe4fd86a`
  （上游 sha 核对通过，14-operator-run-logs.patch 应用成功）；
  `docker save | ssh yidong-36 docker load` 传到 36。
- `/opt/dify/deploy/.env` 先备份（`.env.bak-web-observe-*`）再改 `DIFY_WEB_IMAGE`，
  `docker compose up -d --force-recreate web` 重建 `deploy-web-1`。
- 容器内实查：`/app/targets/next/` 编译产物含 `复核观察` 新文案
  （server page.js 与 static chunk 均命中）；入口 `http://127.0.0.1:10008/` → 307。
- 9 容器全 Up。用户端需硬刷新拿新 bundle。

## 真实管家端到端（41，已执行）

`tools/verification/operator_e2e.py` 以真实运营账号 `ulink`（公司 UPMS 实签令牌，
非自签）直打 `172.18.0.1:8788`（`X-Business-Entry: operator`）：

- 问句：`帮我分析这笔单的电费2116050516255272961`
- `agent_runs`：`run_ra-hxGiE8l9Aa2TXn6NFJNeReyA2utlkXmMDpC5fc4E`，
  `state=succeeded`、`order_no=2116050516255272961`、
  `chat_binding=operator_order_diagnosis:production-41:1942105476598861824`、
  `exec_config_state=matched`、`route_revision=38`。
- 快照：`delivery_mode=diagnosis`；`answer_review.verdict=allow`、
  `reason=allowed`、`model=deepseek-v4-1-flash-261017`、
  `currencies=["元"]`、`station_names=[]`；答复 953 字正常投递，
  无 resample 轨迹。

即：复核照常执行并落快照为观察字段，答复按 diagnosis 交付——ADR-0252 的
「观察而非闸」语义在线上实跑通过（allow 分支）。

## 验收边界

- 线上只验到 `verdict=allow` 路径；reject/uncertain 仍交付的分支由单测
  （verdict 参数化 ×3、dispatch_raises ×2）与独立 Hack 回执覆盖，
  生产复核拒绝频率列入观察。
- 终局闸拒兜底（completion_refusal + stamp 约定）无线上触发样本，
  由参数化回归覆盖。
- ED4 实机容器验证、llmdoc 同步沿用主补丁清单另行完成。
