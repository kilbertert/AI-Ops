# 369. 彻底退役旧侧边车 tar/systemd 发版资产、补齐工作台图标与 `X-Request-Id` 跨跳排障

状态：已采纳；完成简化方案 1–4 落地后的最后清尾，消除仓内/宿主残余的多轨发版与文档分叉，并打通边缘各跳统一请求追踪。

## 上下文

简化 1–4 合入并切换上线后，仍残留四处可进一步收敛的尾项与实机细节：

1. **旧打包资产残余**：`ops/dify-console-bridge/{build-bundle.sh,deploy.sh,aiops-dify-console-bridge.service}` 与 `ops/dify-login-proxy/aiops-dify-login-proxy.service` 仍留在仓库中，36 宿主机也留存已停用的 `/opt/aiops-console-bridge`、`/opt/aiops-login-proxy` 与 `.bak-*` 备份；部分运行手册仍写着手工 `tar.gz` 安装步骤。
2. **独立管理台图标**：`services/aiops-console/` 迁出后沿用了 `i-ri-*` 类名，但未引入对应的 RemixIcon 构建插件，导致界面图标区域呈空白占位。
3. **跨跳排障缺少统一关联键**：浏览器经过 `Nginx → dify-console-bridge → dify-login-proxy` 多跳后，排查 403/502 缺少单次请求贯通的 `X-Request-Id`。

## 决定

1. **删除旧边缘 tar/systemd 发版资产与宿主残留**
   - 从仓库删除 `ops/dify-console-bridge/{build-bundle.sh,deploy.sh,aiops-dify-console-bridge.service}` 与 `ops/dify-login-proxy/aiops-dify-login-proxy.service`。
   - 将 `tests/test_console_bridge_deployment.py` 的导入自包含检查改为直接扫描 [`deploy/dify-36/edge/Dockerfile`](../../deploy/dify-36/edge/Dockerfile)，覆盖 `dify_console_bridge` 与 `dify_login_proxy` 两套服务的可达依赖。
   - 在 36 宿主机清理/归档已停用 systemd unit、旧代码目录与 Nginx `.bak-*` 残骸。
2. **在构建期补齐独立工作台 RemixIcons**
   - 在 `services/aiops-console/` 引入构建期依赖 `@egoist/tailwindcss-icons` 与 `@iconify-json/ri`，通过 `@plugin "./icons.ts"` 编译静态 CSS `mask-image` SVG。
3. **全链路透传与记录 `X-Request-Id`**
   - Nginx 边缘 snippet（`dify-console-publish.conf` 与 `dify-login-proxy.conf`）向后端注入 `X-Request-Id: $request_id` 并回显响应头。
   - `dify_console_bridge` 与 `dify_login_proxy` 校验安全字符集（`[A-Za-z0-9._-]{1,64}`），写入 `[rid=...]` 结构化日志并原样回显响应头。
4. **文档入口与变更归属明确**
   - 在 [`deploy/dify-36/ARCHITECTURE.md`](../../deploy/dify-36/ARCHITECTURE.md) 新增「变更归属速查表」与「`X-Request-Id` 排障方法」，并统一清理关联手册中的旧 `build-bundle.sh`/`deploy.sh` 过时指引。

## 后果

- **单一真相源彻底闭环**：边缘层仅余 [edge/deploy-edge-image.sh](../../deploy/dify-36/edge/deploy-edge-image.sh) 与 [web-patches/deploy-web-image.sh](../../deploy/dify-36/web-patches/deploy-web-image.sh) 两条 ACR `@sha256` 交付链。
- **排障效率提升**：从浏览器 Network 面板取一枚 `X-Request-Id` 即可在容器日志里一次性对齐所有边缘跳。

## 备选方案

1. **保留旧 `build-bundle.sh` / `deploy.sh` 并标记为“废弃提示脚本”**：被否。只要脚本留在仓库中，CI、文档和新接手工程师仍须分辨两条平行发布链；既然 `aiops-dify-edge` 已连续稳定承载全部流量，直接从仓内删除并归档宿主旧目录才算真正收口。
2. **仅用在线 Iconfont/CDN 字体或在 TSX 内手写 inline SVG 替代 `@egoist/tailwindcss-icons`**：被否。内网工作台不应增加外部字体 CDN 依赖，而 `aiops-console` 原代码已普遍使用 UnoCSS/Tailwind `i-ri-*` 语义类名；用 `@iconify-json/ri` 在 Vite 构建期编成静态 CSS `mask-image` 改动最小且零运行时开销。
3. **仅在 Python 侧边车各自生成随机 `request_id` 而不由 Nginx 统一注入**：被否。浏览器触发的一次操作可能横跨 `/console/api/*`、`/aiops/*` 与 `/__aiops/*` 多条 upstream，只在后端各自生成会导致 Nginx 访问日志与 Python 日志无法串查。
