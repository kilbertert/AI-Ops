# 运行日志行级明细（ADR-0251）双端上线记录

- 日期：2026-11-12
- 合并：`587af5d7`（PR #1491 squash）；实现与测试明细见同目录
  `2026-11-12-runlog-detail-v2.md`

## 41 Gateway（CD 自动部署）

- CD run `43202127004` conclusion=success。
- `GET /health`：`{"ok":true,"version":"1.9.1+587af5d75a1e",...}`——版本即合并提交。
- **生产库迁移实证**：现网库 `PRAGMA user_version` 实测 `1 → 2`，
  `agent_runs.tool_summaries_json` 列已补齐，服务启动零阻断。
  这是本次评审/Hack 抓到的「v1 库启动被拒」修复的首个真实环境验证；
  迁移由 `gateway_schema.ensure` 启动路径自动完成，无人工 DDL。

## 36 Dify Web（手工换镜像）

- 本机构建 `aiops/dify-web-patched:run-log-detail-v2-587af5d7`（打补丁源已核对
  上游 sha，`14-operator-run-logs.patch` 应用成功）；`docker save|ssh load` 传到 36。
- `/opt/dify/deploy/.env` 先备份（`.env.bak-runlogv2-*`）再改 `DIFY_WEB_IMAGE`，
  `docker compose up -d --force-recreate web` 重建 `deploy-web-1`。
- 容器内实查：`/app/targets/next/` 编译产物含 `已进行`、`复核通过`、`callNo`
  等新串——v2 渲染确实进镜像，不是缓存旧 bundle。

## 验收边界

- 未做真实 CPMS/节点链路的端到端业务验收；行级内容正确性以 store/投影/发射点
  单测、独立 Hack 回执与上述部署证据为证。
- 摘要只挂同工具最后一个完成事件、工具行恢复「耗时+累计」均为前端呈现层，
  用户需硬刷新拿新 bundle（同上次 stale bundle 教训）。
