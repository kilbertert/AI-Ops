## 2026-10-24 · 建库同步改挂在「添加知识库」弹窗上（ADR-0122 修订）

**上一版为什么作废**：`#1049` 把触发点放在 `GET /v1/dify/knowledge`（我们的清单路由）。
Codex review 指出：那条路由**只有「创建外部知识库」表单会调**
（`ExternalApiSelection.tsx` → `ExternalKnowledgeIdPicker`），而运营要的是编排页的
「添加知识库」——那条走 Dify 自己的 dataset 列表，根本不经过我们的清单路由。
同一次 review 还抓到一个 P1：`libraries_for` 只认 `knowledge_base_id`/`id`，
我传的是 `dataset_id` ⇒ **每一行被静默跳过、同步报成功但零写入**。

**这一版**：打 Dify 前端补丁（`web-patches/1.17.1/02-web-sync-on-knowledge-picker.patch`），
在 `select-dataset/index.tsx`（「添加知识库」弹窗）打开时调一次
`POST /__aiops/knowledge-sync`。

三条纪律，与网关侧那份一致：
1. **失败静默** —— catch 掉，列表照常显示；
2. **每次打开只触发一次** —— `isShow` 由 false 变 true 的那一刻；
3. **有变化才刷新** —— 否则这次打开看到旧列表，运营会以为没生效。

**补丁验证**：`git apply --check` 干净通过；`npx tsc --noEmit` 在我改的两个文件上
**零类型错误**（跑的是 Dify 1.17.1 的真实源码树）。

**后端**：新增桥端点 `_post_knowledge_sync` + 网关 `/v1/internal/knowledge/sync`。
判据 8 条（租户只能来自 workspace 反查 / 空 workspace 不回落 / 上游挂了回
`created:0` / 未配模板即 404 / 只查不建）。

**未做**：重建 Dify web 镜像、换容器、真机验收 —— 按用户决定分两步，第二步单独确认窗口。
