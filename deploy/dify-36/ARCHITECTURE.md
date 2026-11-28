# Dify @ 36 宿主总览与交接索引（唯一架构入口）

> **给新接手工程师的一页纸地图**：理解 `yidong-36`（`https://dify.ranlei.work`）上 Dify 与 AI-Ops 周边组件怎么协同、凭据放在哪、升级走哪条命令，看本文件即可；各历史 `README.md` 仅作细节参考。

## 1. 一张图看清运行拓扑

```
浏览器 (https://dify.ranlei.work)
  │
  ▼
宿主 Nginx (TLS 443 -> 127.0.0.1:10008, 配置见 ops/dify-console-bridge/dify-console-10008.conf)
  ├─ /signin                         ──► 静态自包含登录页 /srv/aiops-console-assets/dify-login-page.html
  ├─ /__aiops/login*                 ──► 127.0.0.1:10119 容器 aiops-dify-login-proxy   [uid=aiopslogin]
  ├─ /__aiops/publish|trial|manage*  ──► 127.0.0.1:10118 容器 aiops-dify-console-bridge [uid=aiopsbridge]
  ├─ /aiops/* (环境与租户工作台)     ──► 127.0.0.1:10118 容器 aiops-dify-console-bridge [console-dist SPA]
  └─ /console/* /api/* /apps /...    ──► 127.0.0.1:10018 Dify 上游容器组
                                           ├─ deploy-web-1  (ACR 镜像 aiops-dify-web@sha256)
                                           ├─ deploy-api-1  (5001 ◄── 127.0.0.1:15001 aiops-inner-api-forward.service)
                                           └─ worker / db_postgres / redis / plugin_daemon / ssrf_proxy / weaviate
```

## 2. 四类组件与职责/隔离边界（不混用、不共用密钥）

| 组件 | 交付物（阿里云 ACR / 宿主机） | 运行形态与 OS 身份 | 核心职责 | 只读/读写目录与密钥 |
|---|---|---|---|---|
| **Dify Web 前端** | `qushi-registry.../release/aiops-dify-web@sha256:...` | Docker (`deploy-web-1`) | Dify 控制台 UI、编排页受管发布条与 RAGFlow 入口卡（ADR-0365 / ADR-0366 / ADR-0368） | 无宿主机私有盘密钥 |
| **控制台发布/管理桥（含独立工作台）** | `qushi-registry.../release/aiops-dify-edge@sha256:...` | Docker (`aiops-dify-console-bridge`, `aiopsbridge`, `--network host --read-only`) | 同源 `/__aiops/*` 发布、撤回、版本、试运行、管家预览及 `/aiops/*` 独立管理工作台静态托管（`services/aiops-console/`） | Env: `/etc/aiops-console-bridge/bridge.env`；只读挂载 `/var/lib/aiops-console-bridge` |
| **公司账号登录代理** | **同上 `aiops-dify-edge@sha256:...`** | Docker (`aiops-dify-login-proxy`, `aiopslogin`, `--network host --read-only`) | 公司账号密码验证码换取 Dify 会话、`mappings.db`、`/__aiops/publish-check` | Env: `/etc/aiops-login-proxy/login-proxy.env`（独享 `DIFY_SECRET_KEY` / `COMPANY_AES_KEY`）；读写挂载 `/var/lib/aiops-login-proxy` |
| **Inner API 转发器** | `/usr/local/bin/aiops-inner-api-forward` | systemd (`aiops-inner-api-forward.service`) | 通过宿主机 `docker inspect` 动态追踪 `deploy-api-1:5001` 并监听 `127.0.0.1:15001` | 需读取 `/var/run/docker.sock`，保留宿主机 systemd |

> **关键安全不变量（ADR-0023 / ADR-0367）**：`console-bridge` 与 `login-proxy` **共享同一份只读 OCI 构建镜像**，但在运行时必须是**两个独立容器、两个不同 Linux UID/GID（`aiopsbridge` vs `aiopslogin`）、两套独立 env 与数据卷**；`aiopsbridge` 永远拿不到 `DIFY_SECRET_KEY` 与 `COMPANY_AES_KEY`。

## 3. 标准发布与回滚命令

- **升级 Dify Web 前端**（修改 `deploy/dify-36/web-patches/**` 后由 `.github/workflows/release-dify-web-image.yml` 自动推送 ACR）：
  ```bash
  bash deploy/dify-36/web-patches/deploy-web-image.sh --image <AIOPS_DIFY_WEB_IMAGE_REPO>@sha256:<64hex>
  bash deploy/dify-36/web-patches/deploy-web-image.sh --rollback
  ```
- **升级 Dify Edge 双侧边车**（修改 `services/dify-*` 或共享依赖后由 `.github/workflows/release-dify-edge-image.yml` 自动推送 ACR）：
  ```bash
  bash deploy/dify-36/edge/deploy-edge-image.sh --image <AIOPS_DIFY_EDGE_IMAGE_REPO>@sha256:<64hex> --service all
  bash deploy/dify-36/edge/deploy-edge-image.sh --rollback --service all
  ```
  升级失败触发自动回滚时，`AUTO_ROLLBACK_FAILED:<服务>` 表示该服务的恢复命令失败，需核对运行状态并人工恢复；脚本仍会尝试恢复其余侧边车，不会报告 `AUTO_ROLLED_BACK`。
- **本地验证 OCI 构建**（不推送 ACR）：
  ```bash
  bash deploy/dify-36/edge/build-edge-image.sh
  ```

## 4. 按需深挖的子文档

- Dify 实例与上游容器基准：[deploy/dify-36/README.md](README.md)
- 前端源码补丁与 Web ACR 流水线：[deploy/dify-36/web-patches/README.md](web-patches/README.md)
- 发布桥路由合同与 Nginx 配置：[ops/dify-console-bridge/README.md](../../ops/dify-console-bridge/README.md)
- 公司登录代理状态机与环境变量说明：[ops/dify-login-proxy/README.md](../../ops/dify-login-proxy/README.md)
- Inner API 转发器设计依据：[ops/dify-inner-api/README.md](../../ops/dify-inner-api/README.md)
