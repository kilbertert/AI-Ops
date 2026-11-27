# 362. 客户环境用户入口以公开 OpenAPI 契约为唯一真源自动生成并门禁对齐

状态：已采纳；补充 ADR-0319（环境用户助手入口凭据分流）与 ADR-0361（Day-2 版本升级），不放开任何管理面、内部回调或单问之外的未公开路由。

## 上下文

ADR-0319 为经 V2.1 自助向导接入的客户环境 HTTPS 域名确立了两项安全约束：

1. 客户侧 Nginx 必须根据 `X-Business-Entry` 完成凭据分流与头清洗，只转发用户/管家助手路径，阻断 `/v1/dify/*`、`/v1/internal/*`、`/v1/agents`、`/v1/operator-*`、`POST /v1/shortcuts` 等管理或内部接口；
2. 必须采用精确 `location =` 与长前缀 `location ^~`，避免干扰客户原有业务路由。

但当时的可转发清单 `ops/nginx/environment-user-ingress-routes.txt` 采用人工维护的 13 条子集白名单，与 Gateway 用户公开契约真源 `src/aiops_diagnostics/gateway/public_openapi.py`（`OPERATIONS`，现为 30 条公开端点）及 H5 清单各自独立演化，导致已开发上线的用户/管家能力在客户环境公网域名持续返回 `404`——包括 `GET /v1/shortcuts`、`GET /v1/assistant/starters`、`GET /v1/banner`、`GET /v1/assistant/agents`、`GET /v1/assistant/questions/{qa_id}/events`（SSE 流）、`/v1/assistant/files*`（附件上传/解析/预览/下载）、`POST .../feedback|regenerate|export`、`PATCH /v1/conversations/{conversation_id}`、`GET /v1/orders/{order_no}/access`、`GET /v1/sites` 与 `/v1/standard/analyses*` 共 18 条接口。同时 `environment-user-ingress-proxy.inc` 全局固定 `client_max_body_size 64k`，会阻断 `POST /v1/assistant/files`（上限 20 MiB）附件上传。

## 决定

1. **建立公开用户契约到客户 Nginx 入口的单一真源**：
   - 新增 `src/aiops_diagnostics/gateway/user_ingress_manifest.py`，直接读取 `public_openapi.OPERATIONS` 与显式声明的兼容路由 `COMPAT_USER_INGRESS_OPERATIONS`（当前仅保留历史客户端已依赖的 `POST /v1/conversations`），共同构成客户环境用户入口的唯一真源。
   - 任何新参数占位符或非 `/v1/` 前缀路径若未在 `PLACEHOLDER_PATTERNS` 声明确定性 `$uri` 形状正则，生成器直接 fail-closed 拒绝，杜绝写出宽松正则。
   - `/v1/orders/{order_no}/access` 复用统一订单标识语法 `ORDER_ID_ATOM` 并额外拒绝路径遍历片段 `..`；`/v1/orders/{order_no}/charging-status*`、`/v1/conversations/{id}/active-order`、`POST /v1/shortcuts` 等非公开操作继续在 Nginx 层被 `404`/`405` 拦截。

2. **确定性生成 `environment-user-ingress-routes.txt` 与 `environment-user-ingress.conf`**：
   - 保留 ADR-0319 全部安全边界（`X-Business-Entry` 标准化分流与 `403` 拒绝、自报头与 `Cookie` 清空、`location ^~ /v1/media/` 签名免头直转、`access_log off`、`location =` + `location ^~` 与客户主站点无冲突共存）。
   - 将 `client_max_body_size` 与 `proxy_read_timeout` 从 `ops/nginx/environment-user-ingress-proxy.inc` 下沉到各 `location` 块：仅 `POST /v1/assistant/files` 放宽请求体至 `25m`，仅 `GET /v1/assistant/questions/{qa_id}/events`（嵌套在 `location ^~ /v1/assistant/questions/` 内）设置 `proxy_read_timeout 300s`、`chunked_transfer_encoding on` 与 `Last-Event-ID` 透传；其余全部接口维持 `64k` 与 `120s`。

3. **CI 双向强对齐与 ADR-0361 发版自动同步**：
   - `tests/operations/test_environment_user_ingress.py` 强制校验仓库内 Nginx 声明与配置必须逐字节等于生成器输出且完整覆盖 `public_openapi.OPERATIONS | COMPAT_USER_INGRESS_OPERATIONS`；并用真实 `nginx` 与真实 Gateway 进程接缝验证放行面、方法隔离、管理面/内部路径拦截、SSE 头透传及附件上传体积限制。
   - 新增或修改任何公开用户接口却未同步更新客户入口片段时，CI 直接失败；已上线客户环境通过 ADR-0361「版本与发版」升级即可自动比对并热重载最新 Nginx 用户入口。

## 替代方案

- **继续遇到哪个 404 再手工补一条白名单**：每轮开发新能力都要踩客户域名 404，既漏检也无法约束跨 PR 漂移，拒绝。
- **在 Nginx 直接敞开整片 `location ^~ /v1/`**：会把 `/v1/dify/*`、`/v1/internal/*`、`/v1/agents` 与写管理方法一起暴露到公网域名，且可能抢占客户业务网关既有 `/v1/` 路径，违反 ADR-0319 安全边界，拒绝。

## 后果

- 客户环境 HTTPS 域名与主干开发的 31 条用户/管家公开入口保持 100% 一致（覆盖 `GET /v1/shortcuts` 等全部 18 条此前缺失能力），而管理面与内部接口维持严格封堵。
- 未来研发新增用户接口后，CI 自动督促通过 `uv run python -m aiops_diagnostics.gateway.user_ingress_manifest --write` 同步入口，升级发版时即随版本包原子下发到所有客户环境。
