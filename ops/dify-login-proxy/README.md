# Dify 登录代理（#772 B / #778）

**这是什么**：运营用**公司账号**进 Dify 控制台的那条路。用户输公司账号、密码与真实验证码，
代理完成公司登录交换、读权限码，然后交给浏览器一枚 **Dify 控制台会话**。

决定的论证在 **ADR-0023**；代码在 `src/aiops_diagnostics/dify_login_*.py`。
本目录只是**部署资产**。

## 拓扑

```
浏览器 ──► <TLS 域名>（宿主 nginx，本目录的 conf）
             ├─ /console/api/…      ──► 127.0.0.1:10018（Dify 自己的 nginx）
             ├─ /                  ──► Dify web
             └─ /__aiops/login     ──► 127.0.0.1:10119（本代理）
                 /__aiops/login/captcha
                 /__aiops/login/logout
```

## 四件东西，装在哪

| 文件 | 装到 | 作用 |
|---|---|---|
| `aiops-dify-login-proxy.service` | `/etc/systemd/system/` | 服务单元（身份 `aiopslogin`，**独立于**发布桥的 `aiopsbridge`） |
| `login-proxy.env.example` | `/etc/aiops-login-proxy/login-proxy.env`（0600） | 全部配置，含两把密钥 |
| `dify-login-proxy.conf` | 控制台 server 块 / snippets | 四跳同源反代（登录/验证码/登出/发布检查） |
| `dify-login-page.html` | `/srv/aiops-console-assets/` | **登录页**（单文件自包含），由 10008 的 `location = /signin` 提供 |
| 代码 | `/opt/aiops-login-proxy/`（venv） | `python -m aiops_diagnostics.dify_login_server` |

**登录页为什么在 `ops/dify-console-bridge/` 的 conf 里接线、而不是本目录**：
`location = /signin` 必须挂在**被两个入口共用**的那一层（10008），否则明文入口
`http://36.156.159.175:10008/signin` 会绕过它。见 `dify-console-10008.conf` 的注释与 ADR-0028。

## 三条硬前置（不满足就别装）

1. **控制台入口已是 TLS**（#776）。`AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN` 非 https 时代理
   **拒绝启动** —— 公司密码不得经明文传输。

   这一条还**不够**，它是启动时的配置断言，证明不了"这一次请求"从哪进来（#849）。
   请求层另有一道闸：登录 POST 带 `X-Forwarded-Proto: https` 才收口令，缺失即拒
   （fail-closed ⇒ 漏配的后果是登录不可用，不是明文放行）。

   > **部署时必须核对**：那两个头由 `ops/dify-console-bridge/dify-console-10008.conf`
   > 提供 —— 顶部用 `realip`（`set_real_ip_from` 只列回环）把真实来源写回
   > `$remote_addr`，并用 `map $realip_remote_addr $aiops_upstream_proto` 算出该上报的
   > 协议；登录 snippet 转发的就是这两个已修正的值。**两处缺一不可**：snippet 里
   > 直接写 `$scheme` 是错的（这一层明文监听，`$scheme` 恒为 http，会把 TLS 入口写的
   > https 覆盖掉）。上线判据：`https://dify.ranlei.work` 能登录、`http://...:10008`
   > 提交口令得到「请使用 https 登录」，两者都成立才算装对。

2. **公司侧权限码已存在**（#777），且 `AIOPS_LOGIN_PROXY_COMPANY_CLIENT_TYPE` 与那两个
   权限码所挂的 `client_type` **一致**；否则公司侧取到空角色集，表现为"你没有权限"。
3. **公网已封掉 Dify 原生账号入口**（#783）：`/console/api/login`、找回密码、邮箱验证码、
   注册、`/activate`。不封，公司登录就不是唯一入口。

## 两处**已知且被接受**的行为（别当成故障排查）

1. **refresh 永远失败，这不是缺陷。** Dify 的 refresh token 是 Redis 里的一次性随机串，
   而本代理（#778 的自签模型）把它下成与 access 同值的 JWT ⇒ `POST /console/api/refresh-token`
   必然 401。**实测确认前端不会因此把有有效 access cookie 的人打回去**（带
   `__Host-access_token` 时 `GET /` 与 `GET /apps` 都是 200）。所以会话到期后落到的是**我们的**
   登录页 —— 这正是要的行为。**会话寿命因此是硬上界**：`AIOPS_LOGIN_PROXY_ACCESS_MINUTES`。
2. **`__Host-` 前缀在 https 下是确定的**（#776 起控制台就是 https，且本部署未设
   `COOKIE_DOMAIN`）。共三处要带前缀：下发、登出清理、以及 `dify_login_provision` 里
   开户时那个写死的 csrf cookie 名 —— 第三处漏了会让**新账号开不出来**而老账号没事。

## 密钥与后果（ADR-0023 的代价）

`AIOPS_LOGIN_PROXY_DIFY_SECRET_KEY` 让代理能签**任何** Dify 账号（含 owner）的会话，
且那把 key 同时保护 Dify 库里存着的工具/模型凭据。**代理被攻破 = 全部映射账号 + 全部
Dify 侧存储凭据失守。** 因此：

- 代理只监听回环、经控制台源同源反代，不对外另开端口；
- 代理进程**不落盘**公司密码或令牌，日志只记公司用户 id 与结果码；
- 轮换步骤见 ADR-0023（**必须** Dify 与代理一起重启）。

## 映射表

`/var/lib/aiops-login-proxy/mappings.db`，键是**公司用户 id**，附 Dify 账号 id、邮箱
（`<id>@aiops-login.invalid`，不可投递、确定性生成）、权限码快照、状态、时间。
**表里没有密码** —— 自签模型下不存在要保管的密码。
