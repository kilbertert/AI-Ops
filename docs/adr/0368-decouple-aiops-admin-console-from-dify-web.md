# 368. 将 `/aiops/*` 独立管理工作台从 Dify Next.js 剥离，由 `aiops-dify-edge` 同源托管

状态：已采纳；接续 ADR-0365（`dify-web` 走公司 ACR 发布）、ADR-0366（退役 Nginx `sub_filter`）和 ADR-0367（侧边车收敛至 `aiops-dify-edge`），把环境与租户管理控制台彻底迁出 Dify Next.js 编译链路。

## 上下文

在此之前，`/aiops/environments` 及其版本与发版、自助开通向导界面（`aiops-environments.tsx`、`aiops-environment-releases.tsx`、`aiops-onboarding-wizard.tsx`）被作为补丁静态拷贝进上游 `langgenius/dify@1.17.1` Next.js 源码树一并编译：

1. 任何管理后台 UI 迭代都要触发完整的 Dify Web 全量 Next.js/webpack 重编译（约 6 分钟），认知面和迭代延迟高度耦合并被 Dify 内部包 `@langgenius/dify-ui/*` 与 `@/next/navigation` 拖住；
2. 事实上，这三份页面完全通过 `/__aiops/manage` 同源桥接口与 Gateway 通信，不依赖 Dify 会话态之外的任何 Dify Web 运行时逻辑。

## 决定

1. **在 `services/aiops-console/` 建立独立的 Vite + React 19 + Tailwind v4 SPA**
   - 原样迁入 `aiops-environments.tsx`、`aiops-environment-releases.tsx`、`aiops-onboarding-wizard.tsx`，移除 `useRouter()` 依赖并拆出与 Dify 浅色主题一致的轻量化 UI 原语及 `manage.ts`（含 `401` 自动跳转 `/signin?redirect=...`）。
   - 构建产物输出至 `/aiops/` 基准路径（`console-dist`）。

2. **由 `aiops-dify-edge`（`dify-console-bridge`）同源托管 `/aiops/*` 并保持 Dify 会话门禁**
   - `deploy/dify-36/edge/Dockerfile` 升级为两阶段构建：`node:22-slim` 编译 `services/aiops-console/`，`python:3.12-slim` 只读搭载 `/opt/aiops-edge/console-dist`。
   - `dify_console_bridge` 新增 `GET /aiops` / `GET /aiops/environments`（无 Dify 会话 Cookie 时直接返回 `307` 跳转 `/signin?redirect=%2Faiops%2Fenvironments`）以及 `GET /aiops/assets/*`（严格目录防穿越限制）。
   - `ops/dify-console-bridge/dify-console-publish.conf` 新增 `location ^~ /aiops` 反代到 `127.0.0.1:10118`，不经过 Dify 上游 Next.js 容器。

3. **`dify-web` 侧边栏仅保留原生 `<a>` 整页跳转锚点，大幅瘦身 `web-patches`**
   - 删除 `deploy/dify-36/web-patches/1.17.1/` 中已迁出的 4 份环境管理源/测试文件，保留编排页内嵌入口（`ManagedPublish`、`WorkflowManagedStrip`、`AiopsKnowledgeOpenCard` 等）。
   - `17-environment-directory-tab.patch` 移除 `app/(commonLayout)/aiops/environments/page.tsx`，并在 `nav-link.tsx` 为 `/aiops/` 路由使用原生 `<a href="/aiops/environments">` 整页切换，避免 Next.js RSC 请求落空。

## 替代方案

- **另起独立域名（如 `admin.ranlei.work`）部署**：会打破 Dify `__Host-access_token` / `__Host-csrf_token` 同源会话边界，迫使管理台重新引入跨域认证或独立密钥，拒绝。
- **保留 `/aiops/environments` 寄生在 Dify Next.js 内**：每次修改环境/发版台界面都要等待完整 Dify Next.js 重编译并紧耦合上游 `@langgenius/dify-ui` 内部重构，拒绝。

## 后果

- **研发节奏解耦**：管理工作台迭代仅重建轻量 `aiops-dify-edge`（约 30–40 秒），不再触发耗时的 Dify Web 重编。
- **Dify 升级包瘦身**：`deploy/dify-36/web-patches/1.17.1/` 删除超 8,000 行后台页面代码与重型测试，升级上游 Dify 版本时的补丁维护面显著下降。
