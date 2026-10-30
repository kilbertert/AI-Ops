# 进度

基线：`origin/main` @ `b5d46610`（2026-10-30）。分支 `feat/dual-entry-onboarding`。
中断后先读本文件，不重复已完成项。阻塞项见 [BLOCKED.md](BLOCKED.md)。

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 任务0 基线复跑 | 完成 | 53 passed, 1 warning（与任务书基线一致） |
| 任务0 环境核验 | 完成 | 41 `/health` = `1.0.0+e30318737089`，`business_mutations=disabled`；`gh` 登录 `kilbertert`；规划提交 `5cd87f64` 核对一致 |
| 文档 PR | 完成 | PR #1221 合并 `f5b6cd56`（规划与任务书入主干） |
| T1 规格与验收合同 | 完成 | `implementation-spec.md` / `acceptance.feature` / `tasks.md` / `qa-plan.md` |
| T2 目录区分端/场景 | 完成 | `dify/dual_entry.py`、`self_service_store.py`（含回归修复） |
| T3 目录 API 双端来源 | 完成 | `catalogue.entries[]`，顶层 `sources` 兼容 |
| T4 按端分派开通 | 完成 | `scope`/`sources`、逐端状态、`partial`、重试续跑 |
| T5 客户端应用准备 | 完成 | 复制已发布快照 + 知识整段重绑 + 幂等导入 |
| T6 服务凭据自动落盘 | 完成 | 先快照后创建再核对；丢回执对账；0600 原子写 |
| T7 受管客户端发布目标 | 完成 | 受管行为第一权威，文件兼容，冲突拒绝 |
| T8 草稿预览 | 完成 | 合成 draft 路由，不写正式指针 |
| T9 双端发布/撤回/恢复 | 完成 | 复用控制台发布同一条推进路径 |
| T10 渠道与就绪口径 | 完成 | `[[channels]]` 声明；四件事分开报 |
| T11 前端双端入口 | 进行中 | Dify web patch 组件 |
| T12 纳管与部署 | 待开始 | 41 部署 + 现役资源纳管 |
| T13 真实双端验收 | 待开始 | 真实消费者/员工账号 |

## 自动化结果（本机）

- `uv run pytest -n 4`：3620 passed / 9 skipped（既有跳过）。
- 新增 `tests/dify/test_dual_entry.py` 26 项、`tests/consumer/test_consumer_draft_trial.py` 3 项。
- `ruff check` / `ruff format --check` 通过。
- `deploy/m2-live-03/images.lock.json` 已按仓库流程重建镜像并写回摘要。
