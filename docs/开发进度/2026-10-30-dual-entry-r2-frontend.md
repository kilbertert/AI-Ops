# 双端开通第二轮：真实 Chatflow 页接入与首页入口

日期：2026-10-30（第二轮）。分支 `feat/dual-entry-r2`。

## 独立验收指出的问题（R3）

第一轮只把 `ManagedPublish` 挂到 `app/configuration/configuration-view`，而固定 1.17.1 源码里
**客服应用的编辑页是 `/app/[appId]/workflow`，渲染 `WorkflowApp`** —— 那一页上没有发布入口。
同页的预览发送也统一写 `/__aiops/operator-preview`，没有 Chatflow 对应的接线。首页也没有
规划要求的"客户 AI 服务"入口。

## 本片改动

| 文件 | 改动 |
| --- | --- |
| `aiops-workflow-managed.tsx`（新增） | 编排页顶部的受管条：读 `useParams().appId` → `useManagedApp`，渲染端标签、正式版本、渠道待接入提示与发布按钮；**不受管的应用返回 null**，普通 Dify 应用一行都不改 |
| `06-workflow-and-home-entry.patch`（新增） | 在 `[appId]/workflow/page.tsx` 挂上受管条；在 `features/home/home-content/home-content.tsx` 挂上 `TenantSetup`（首页入口） |
| `aiops-workflow-managed.browser.spec.tsx`（新增） | 2 项：受管条在编排页渲染并按**页面看到的草稿摘要**发布；不受管应用不渲染 |

发布走的是同一份 `ManagedPublish` + 同一条桥 + 同一个 API；客户端应用额外把
`status.draft_sha256` 作为 `draft` 发回，与后端的摘要校验对齐（R6）。

## 验证

固定 1.17.1 源码 + 全部 web-patches，`aiops-workflow-managed.tsx`、`aiops-management.ts(x)`
复制到 `web/app/components/`、`web/service/` 后：

```
npx vitest --project browser --run app/components/aiops-workflow-managed.browser.spec.tsx
#   Tests  2 passed (2)
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx
#   Tests  10 passed (10)
```

## 未完成

- **没有构建并部署 web 镜像**，因此没有真实页面证据：本片只证明"组件在编排页被挂载且行为正确"，
  不证明"41/36 上的控制台已经能点"。部署步骤与所缺条件见 BLOCKED。
- 整页渲染（ReactFlow 画布 + 受管条同页）没有在浏览器里跑过：Dify 的 workflow 页依赖大量
  上下文，本片用组件级渲染 + 真实页面挂载点（patch）来覆盖，边界如实记在这里。
