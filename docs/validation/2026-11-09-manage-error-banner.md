# 2026-11-09 管理操作报错透出真实原因（PR #1445）验收记录

## 一、确定性检查（可复现）

前端在 `.scratch/dify-1.17.1/web`（dify 1.17.1 打补丁后的工作树，vitest
browser project，node_modules 齐备）；后端在交付 worktree
`/home/claude/Projects/.worktrees/AI-Ops-manage-error-banner`
（分支 `fix/manage-error-banner`，基于 `origin/main` `4a493ce5`）。

| 命令 | 结果 |
| --- | --- |
| `node_modules/.bin/vitest run --project browser app/components/aiops-management.browser.spec.tsx`（最终代码） | **15 passed / 15**（13.29s） |
| 同上，`aiops-workflow-managed.browser.spec.tsx` | 6 passed / 6 |
| 同上，`--project unit .../aiops-workflow-header.spec.tsx` | 1 passed / 1 |
| `uv run pytest tests/execution/test_node_publication.py tests/dify/test_dual_entry.py -o addopts=""` | **46 passed**（41.03s） |

### 新增的长期检查（契约规格，`aiops-management.browser.spec.tsx` +4）

1. `ROUTE_BINDING_RETRACTED` → 必须显示「该版本已撤回…恢复历史版本」，
   拦截「撤回重发再被伪装成登录问题」的回归。
2. 未收录 code + 后端 message → 显示 `message（CODE）`，拦截「新 code
   又退化成笼统兜底」。
3. 裸 code 无 message → 断言**包含 code 本身且不包含「重新登录」**——
   这是本缺陷的签名，兜底永远不许再编会话原因。
4. 502 非 JSON 响应 → 按状态码描述，拦截 SyntaxError/TypeError 原文上屏。

### Review 驱动的两处修正（Devin Review on #1445，全部对照源码核实）

- `SETUP_APP_CONFLICT` 原译文「已被其他任务占用，请刷新后重试」被指出误诊：
  `self_service_store.py:248/266/282` 三处 raise 全是登记身份冲突（归属/端/
  scenario 不一致），刷新不可解。已订正为登记冲突文案；真正的占占用语留给
  `SETUP_BUSY`/`SETUP_TENANT_BUSY`。
- `node_publication.py:361` 把 `RouteBindingError` 以 `str(exc)` 包成
  `NodeRouteConflict`，于是 `code` 槽位透出的是英文 message
  （'route binding moved'/'route binding vanished'/'unknown engine'），
  任何译文表都不命中。改为 `exc.code`，透出稳定 `ROUTE_BINDING_*` code。
  `RouteBindingError` 定义为 `super().__init__(message); self.code = code`，
  无 `__str__` 覆盖，该判断经源码核实。

## 二、Hack 反例

**未执行。** 按 `docs/agents/verification-workflow.md`，独立 Hacker 验收须由另一
会话完成，本记录不代替该步骤。供反例会话使用的方向：

1. 把 `failureText` 的 detail 提取行删掉 → 契约规格 2/3 变红。
2. 把 `manage()` 的 `e instanceof TypeError` 放宽成全捕获 → 既有规格
   「create 第一次抛 `网络中断`」变红（替身自抛 Error 被吞成连接文案）。
3. 把 `node_publication.py` 改回 `str(exc)` → `test_dual_entry.py` 中
   NodeRouteConflict handler 相关用例仍是 409 但 code 变英文短句；
   需配合 grep 断言 `ROUTE_BINDING_` 前缀才能钉住——**已知盲区**，
   建议补一条「publish 冲突时 code 前缀为 ROUTE_BINDING_」的 API 级测试。

## 三、用户路径与未验范围

- 用户路径：Dify 控制台「受管发布」→ `POST /__aiops/manage` → 桥 →
  `/v1/dify/management/publish`。报错文案只在最后一个 UI 层变化，
  后端拒绝语义、信封形状、状态码均未变（`exc.code` 修正让 code 从英文短句
  变回稳定词表，属于把泄漏的实现细节收回契约内）。
- **未完成业务验收**：没有在 41/36 真实控制台复现「已撤回版本重发」的原报错
  截图对照；生产生效还需以合入版重建 dify web 镜像并 `--force-recreate`。
- scratch 树测试后已恢复快照；`deploy/dify-36/web-patches/work` 无
  node_modules，前端类型检查实际由镜像构建期的 dify 工具链承担，本仓库
  CI 不跑 vitest。
