# 进度

基线：`origin/main` @ `8a995897`（2026-10-30 开工时 `4af09893`，期间 rebase 到 `b5d46610`）。
最后更新：2026-10-30 21:0x（北京时间）。
中断后先读本文件，不重复已完成项。阻塞项与所缺条件见 [BLOCKED.md](BLOCKED.md)。

## 状态总表

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 任务0 基线复跑 | 完成 | 53 passed, 1 warning（与任务书基线一致） |
| 任务0 环境核验 | 完成 | 41 `/health` = `1.0.0+e30318737089`；`gh` 登录 `kilbertert`；规划提交 `5cd87f64` 逐字核对 |
| 文档 PR | 完成 | #1221 → `f5b6cd56` |
| T1 规格与验收合同 | 完成 | `implementation-spec.md` / `acceptance.feature` / `tasks.md` / `qa-plan.md` |
| T2 目录区分端/场景 | 完成 | #1224 → `8a995897`；#1226 修启动冲突 |
| T3 目录 API 双端来源 | 完成 | 同上 |
| T4 按端分派开通 | 完成 | 同上 |
| T5 客户端应用准备 | 完成（契约级） | 同上 |
| T6 服务凭据自动落盘 | 完成（契约级） | 同上 |
| T7 受管客户端发布目标 | 完成（契约级） | 同上 |
| T8 草稿预览 | 完成（契约级） | 同上 |
| T9 双端发布/撤回/恢复 | 完成（契约级） | 同上 |
| T10 渠道与就绪口径 | 完成（契约级） | 同上 |
| T11 前端双端入口 | 完成（浏览器用例级） | #1225 → `8161b4f4`；**镜像未部署** |
| T12 纳管与部署 | 部分完成 | Gateway 已部署并回读；Dify web 镜像未构建/部署；渠道声明未配置 |
| T13 真实双端验收 | **未完成** | 缺 Dify 前端部署与真实页面开通，见 BLOCKED |

## 部署记录（真实环境）

| 时间（北京） | 动作 | 结果 |
| --- | --- | --- |
| 20:08 | CD 部署 `8a995897` | **自检失败**，主机自动回滚到 `1.0.0+e30318737089`，服务 active（见 validation 记录） |
| 20:45 | CD 部署 `ffa9ed64` | 成功；`/health` = `1.0.0+ffa9ed645e12`；`dual_entry.py`/`service_keys.py`/`self_service_store.py` 在 `/opt/aiops-41/src/…` 就位（mtime 20:45:26） |
| 20:5x | 线上只读核对 | 匿名 `/v1/dify/management/catalogue`、`/operations` = 403；`/v1/openapi.json` = 200；`/v1/runs` = 404；`/v1/faq/catalog`、`/v1/assistant/questions` 匿名 = 401 |
| 20:5x | 受管目录只读查询 | `operator_apps` 5 行，全部 `entry=operator`、`scenario=operator_order_diagnosis`；其中 3 行由启动纳管写入 |
| 21:0x | CD 部署 `fix/dual-entry-adoption-env`（#1227） | 排队中；生产保持 `1.0.0+ffa9ed645e12` |

## 自动化结果（本机）

```bash
uv run pytest -n 4                                       # 3625 passed, 9 skipped（既有跳过）
uv run pytest -n 0 tests/dify/test_dual_entry.py \
  tests/consumer/test_consumer_draft_trial.py \
  tests/dify/test_self_service_adoption.py               # 26 + 3 + 5 = 34 passed
uv run ruff check src tests && uv run ruff format --check src tests
uv run python tools/verification/check_dual_entry_management.py   # all checks passed
```

浏览器用例（固定 1.17.1 源码 + 全部 web-patches）：

```bash
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx
# Test Files 1 passed (1) / Tests 10 passed (10)
```

## 真实环境上**已证明**的

- Gateway 部署后重启即恢复：`/health` ok、`business_mutations=disabled`、服务 active。
- 启动纳管在**生产库**上真的执行了（5 条受管行，其中 3 条 actor=`managed-adoption`）。
- 管理面在真实进程上 fail-closed：匿名与错密钥一律 403。
- 现役入口仍然只对已认证会话开放（`/v1/faq/catalog`、`/v1/assistant/questions` 匿名 401），
  退役路由仍然 404 —— 本次改动没有放宽任何既有边界。

## 真实环境上**尚未证明**的

- 页面开通（A/B/E/K 的真实动作）：Dify web 镜像未构建部署，且没有平台账号登录。
- 真实消费者会话（G1/G2/H/J1）：需要在原 H5 上用真实账号跑完整链路。
- 真实员工会话（I）：同上，原管家 H5。
- 渠道状态（J2 的真实取值）：41 未配置 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE`，
  也没有声明 `[[channels]]`。
