# 366. 退役 Nginx `sub_filter` 野生 JS 注入脚本，前端定制统一收敛进 `web-patches`

状态：已采纳；配合 ADR-0365（`dify-web` 阿里云 ACR 标准分发）正式下线 Dify 控制台的运行时 HTML 脚本注入层，后端 `/__aiops/*` HTTP 桥接口与权限模型保持不变。

## 上下文

在此之前，Dify 控制台前端扩展同时存在两套互相重叠的修改面：

1. **源码编译层（`deploy/dify-36/web-patches/`）**：已通过 `04-self-service.patch`、`06-workflow-and-home-entry.patch` 与受管组件接管应用页「受管发布」、工作流头部受管发布条及管家原生预览，并且已接入 ADR-0365 ACR 自动发布流水线。
2. **Nginx `sub_filter` 运行时注入层（`ops/dify-console-bridge/dify-console-10008.conf`）**：每次 HTML 响应关闭 upstream `gzip` 并往 `</body>` 强塞 `/__aiops/publish-button.js`（拼接 `operator-native-preview.js`）和 `/__aiops/dify-knowledge-open.js`：
   - 其中 `dify-publish-button.js` 与 `operator-native-preview.js` 已被源码层 `ManagedPublish` / `WorkflowManagedStrip` / `debug-with-single-model` 完全覆盖，仅靠每 500ms `setInterval` 查 `[data-aiops-managed]` 自删避让，徒增认知负担与页面轮询；
   - 仅剩 `dify-knowledge-open.js` 的「知识库页右下角打开 RAGFlow 浮动卡」尚在使用该通道。

## 决定

1. **迁移唯一在用能力到 React 原生组件**
   - 新增 [`deploy/dify-36/web-patches/1.17.1/aiops-knowledge-open.tsx`](../../deploy/dify-36/web-patches/1.17.1/aiops-knowledge-open.tsx)（`AiopsKnowledgeOpenCard`），基于 Next `usePathname()` 仅在 `/datasets` 或 `/datasets/...` 挂载，保留内存级关闭状态与同步 `window.open('about:blank')` 防弹窗拦截逻辑，消除 500ms `setInterval` 轮询。
   - 在 `18-liquid-glass-theme.patch` 的 `web/app/layout.tsx` 中挂载 `<AiopsKnowledgeOpenCard />`，并纳入 `build-web-image.sh` 与 `publish-web-image.sh` 冒烟检查。

2. **彻底删除 Nginx `sub_filter` 与三份历史 JS 资产**
   - 删除 `ops/dify-console-bridge/{dify-publish-button.js,operator-native-preview.js,dify-knowledge-open.js}` 及对应纯离线脚本单测。
   - 从 `ops/dify-console-bridge/dify-console-10008.conf` 移除 `sub_filter`、`sub_filter_once`、`sub_filter_types`、`proxy_set_header Accept-Encoding ""`、`gzip off` 以及 `/__aiops/publish-button.js`、`/__aiops/dify-knowledge-open.js` 两个 `alias` location。
   - 从 `ops/dify-console-bridge/{build-bundle.sh,deploy.sh}` 移除 JS 资产打包、拼接与安装逻辑。

## 替代方案

- **继续保留 `dify-knowledge-open.js` 挂在 Nginx `sub_filter`**：意味着全站 HTML 都要维持 `gzip off` 与双层改端边界，新接手工程师仍须同时理解前端补丁与 Nginx `sub_filter` 两条完全不同的 UI 通道，拒绝。
- **彻底删除知识库 RAGFlow 入口卡**：`/datasets` 页面运营仍需要一键跳转至对应租户的 RAGFlow 管理界面，不应回退现有可用功能，拒绝。

## 后果

- **单一真相源**：Dify 控制台所有前端 UI 修改（包括知识库页跳转卡、受管发布条、独立管理台）全部统一归属 `deploy/dify-36/web-patches/` 并经 ACR `@sha256` 发布。
- **恢复正常 HTTP 压缩透传**：`10008` 代理层不再为了替换 `</body>` 而强制禁用 gzip。
