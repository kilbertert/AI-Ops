# AI-Ops Gateway 架构与部署

## 目标

便携包只负责客户端交互和进度显示。固定服务器上的 Gateway 才负责数据库访问、SSH 隧道、Codex provider、运行状态和事件存储。

```mermaid
flowchart LR
    W[Windows 便携包] -->|HTTPS + 设备令牌| G[AI-Ops Gateway]
    L[Linux 便携包] -->|HTTPS + 设备令牌| G
    G --> DB[(MySQL / TDengine / Redis)]
    G --> C[Codex API]
    G --> S[(run / event 持久存储)]
```

客户端永远不接触数据库密码、SSH 私钥或 provider API key。数据库访问仍复用现有固定参数化查询、TDengine 严格只读代理、Redis ACL 和 Codex thin harness。

## 行业依据

- [NIST SP 800-207 Zero Trust Architecture](https://csrc.nist.gov/pubs/sp/800/207/final)：不能因为设备位于某个网络就隐式信任；用户和设备必须在访问资源前分别认证和授权。
- [RFC 8628 OAuth 2.0 Device Authorization Grant](https://www.rfc-editor.org/rfc/rfc8628)：适合无浏览器或输入受限的设备，通过另一台设备完成一次性授权。
- [RFC 8705 OAuth mTLS](https://www.rfc-editor.org/rfc/rfc8705)：生产环境可使用证书绑定令牌，避免被复制的 bearer token 被重放。
- [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)：集中管理、最小权限、自动轮换和秘密使用审计。
- [HashiCorp Vault Database Secrets Engine](https://developer.hashicorp.com/vault/docs/secrets/databases)：优先为数据库访问签发短期动态凭据，而不是在客户端分发长期密码。
- [Microsoft Credential Locker](https://learn.microsoft.com/en-us/windows/apps/develop/security/credential-locker) 与 [Freedesktop Secret Service](https://specifications.freedesktop.org/secret-service-spec/latest/)：客户端令牌应进入操作系统凭据库，而不是明文配置或漫游文件。
- [Temporal Durable Execution](https://docs.temporal.io/temporal)：跨机器和故障恢复需要持久事件历史；本项目 MVP 先用 SQLite/WAL 事件表，接口预留 PostgreSQL/Temporal 演进空间。

## 当前实现

### 服务端

`aiops-gateway` 提供：

- `GET /health`：非敏感健康检查。
- `POST /v1/enroll`：兑换一次性高熵注册码，注册码只能使用一次。
- `POST /v1/runs`：创建只读诊断 run，真实执行发生在 Gateway 所在服务器。
- `GET /v1/runs`、`GET /v1/runs/{run_id}`：按 workspace 隔离的 run 查询。
- `GET /v1/runs/{run_id}/events`：按 sequence 增量同步事件。
- `GET /v1/runs/{run_id}/events/stream`：SSE 实时同步事件。

服务端 SQLite 只保存注册码哈希、设备令牌哈希、run 元数据、脱敏结果和事件元数据，不保存原始 API key。证据正文仍保留在服务器私有 run workspace，不通过 Gateway 事件接口暴露。

当前 MVP 的注册码由服务器管理员本地签发：

```bash
uv run aiops-gateway issue-enrollment --workspace ops --tenant-id tenant-a
uv run aiops-gateway serve
```

Gateway 进程通过 `AIOPS_GATEWAY_SERVER_CONFIG_FILE` 加载服务器私有 `production.env`。该文件只应存在于固定服务器，不应复制到便携包。

用户级 systemd 服务必须显式固定应用配置和数据根目录，不能依赖用户管理器继承的通用 `XDG_CONFIG_HOME/XDG_DATA_HOME`。否则 key slot 可能错误解析到 `$XDG_CONFIG_HOME/keys`：

```env
AIOPS_CONFIG_HOME=/home/claude/.config/aiops-diagnostics
AIOPS_DATA_HOME=/home/claude/.local/share/aiops-diagnostics
AIOPS_GATEWAY_SERVER_CONFIG_FILE=/home/claude/.config/aiops-diagnostics/production.env
AIOPS_CODEX_KEY_SLOT=psydo-primary
```

### 客户端

新电脑只需一次注册：

```powershell
.\aiops.exe remote enroll --url https://aiops.example.com --code-file C:\secure\gateway-enrollment.code
.\aiops.exe remote doctor
.\aiops.exe remote runs
.\aiops.exe remote diagnose "订单 123 金额异常" --json
```

Windows `cmd.exe` 示例（便携包解压到 `D:\aiops`）：

```cmd
cd /d D:\aiops
dir D:\gateway-enrollment-ops.code
aiops.exe remote enroll --url https://aiops.example.com --code-file D:\gateway-enrollment-ops.code
aiops.exe remote doctor
aiops.exe remote runs
aiops.exe remote diagnose "订单 123 金额异常" --json
```

注册码只能兑换一次。若文件名或路径不正确，客户端会在本地文件检查阶段返回 `File ... does not exist`，此时注册码尚未被兑换；使用 `dir` 检查实际文件名后重试。也可以省略 `--code-file`，让客户端隐藏提示输入注册码。注册成功后删除本地注册码文件，不要再次运行 `remote enroll`。

注册后，客户端只保存 Gateway URL、设备 ID、workspace ID 等 profile，以及设备令牌。令牌优先使用 Windows Credential Locker / Linux Secret Service，通过 `keyring` 访问；没有系统凭据库时才回退到当前用户私有文件，并保留 `0600/Windows DACL` 校验。

同一 workspace 的 Windows 和 Linux 设备查询同一套 run/event 数据，因此换设备后仍能用 `remote runs`、`remote show` 和 `remote events` 查看进度。

`remote doctor` 只调用公开的 `/health` liveness 接口，不证明设备令牌仍有效；要验证注册后的身份，请执行 `remote runs`。诊断出现 `interrupted` 或 `failed` 时，`remote show RUN_ID` 会显示脱敏的 `error_type` 和 `error_message`，`remote events RUN_ID --after 0` 可追溯排队、worker 启动、provider 调用和中断原因。

## 安全边界

- ZIP 不包含数据库凭据、SSH 私钥、provider key 或 Gateway bearer token。
- 客户端不能提交任意 provider URL、任意数据库查询、任意 fixture 路径或业务动作。
- 设备注册 code 和设备 token 只在传输/兑换时出现，服务端只存 SHA-256 哈希。
- 租户绑定在注册码上；设备请求其他租户时被拒绝。
- 服务端 key slot 通过 `AIOPS_GATEWAY_ALLOWED_KEY_SLOTS` 白名单限制，恢复和执行仍比较固定 provider endpoint。
- Gateway 只同步状态和证据 ID 等元数据，不同步 evidence payload、密码或内部绝对路径。
- run 元数据中的用户反馈会在服务端持久化前脱敏；客户端 `show` 只得到诊断合同结果，不得到服务器证据正文。

## 生产硬化要求

当前代码是可运行的单节点 Gateway MVP，不应直接暴露到公网。正式部署前必须完成：

1. 在 Nginx/Traefik 或云负载均衡后启用 TLS，Gateway 只监听 loopback 或内网地址。
2. 把一次性注册码替换为企业 OIDC Device Flow；高安全环境增加 mTLS 或证书绑定 token。
3. 用 Vault/KMS 管理服务端数据库和 provider key，数据库账号改为短期动态凭据；不把长期密码放在客户端环境变量。
4. 将 SQLite/WAL 替换为 PostgreSQL，并为事件表、workspace、device、revocation 和审计日志建立备份与保留策略。
5. 增加反暴力、速率限制、设备撤销、密钥轮换、审计导出和管理员审批。
6. 需要跨节点 durable worker 时再引入 Temporal；第一版不把 Temporal 作为便携包运行时依赖。

这些硬化完成前，Gateway 仅适合内网、测试和受控工程师验收，不宣称已经完成生产多租户认证。
