# 36 上的 Dify 边缘与前端改动：哪些改动由哪条 ACR 流水线负责

> 本表已按 ADR-0365 ~ 0369 更新：旧手工 `build-bundle.sh`、`deploy.sh`、systemd 与 `sub_filter` 注入脚本已全部退役；全量总览见 [`deploy/dify-36/ARCHITECTURE.md`](../../deploy/dify-36/ARCHITECTURE.md)。

## 改动归属表（合并进 `main` 后如何到达 36）

| 改了什么 | 触发哪条 ACR 构建 | 部署到 `36` 的命令 |
|---|---|---|
| `services/aiops-console/**`（`/aiops/*` 管理工作台）<br>`services/dify-console-bridge/**`<br>`services/dify-login-proxy/**` | `.github/workflows/release-dify-edge-image.yml`<br>产出 `aiops-dify-edge@sha256:...` | `bash deploy/dify-36/edge/deploy-edge-image.sh --image <EDGE_REF> --service all` |
| `deploy/dify-36/web-patches/**`（Dify 编排页内嵌扩展 / RAGFlow 入口卡） | `.github/workflows/release-dify-web-image.yml`<br>产出 `aiops-dify-web@sha256:...` | `bash deploy/dify-36/web-patches/deploy-web-image.sh --image <WEB_REF>` |
| `ops/dify-console-bridge/dify-console-publish.conf`<br>`ops/dify-login-proxy/dify-login-proxy.conf` | Nginx snippet 变动需同步到 `36:/www/server/panel/vhost/nginx/snippets/` | `dev-host cp 36 ... --artifact-sha256` + `nginx -t && nginx -s reload` |

## 怎么确认装没装（与跨跳排障）

```bash
# 1. 核对 36 运行中的不可变 @sha256 镜像与只读身份隔离
dev-host exec 36 --allow-service-exec -- "docker inspect --format '{{.Name}} User={{.Config.User}} Readonly={{.HostConfig.ReadonlyRootfs}} Image={{.Config.Image}}' deploy-web-1 aiops-dify-console-bridge aiops-dify-login-proxy"

# 2. 按浏览器响应头中的 X-Request-Id 对齐查边缘双容器日志
dev-host exec 36 --allow-service-exec -- "docker logs --since 15m aiops-dify-console-bridge 2>&1 | grep <X-Request-Id>"
dev-host exec 36 --allow-service-exec -- "docker logs --since 15m aiops-dify-login-proxy 2>&1 | grep <X-Request-Id>"
```

## 回滚

- Edge 侧边车与独立管理台：`bash deploy/dify-36/edge/deploy-edge-image.sh --rollback --service all`
- Dify Web 前端：`bash deploy/dify-36/web-patches/deploy-web-image.sh --rollback`
- Nginx snippet：还原对应 `*.bak-*` 后执行 `/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload`
