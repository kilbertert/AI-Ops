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
| `dify-login-proxy.conf` | 控制台 server 块 / snippets | 三跳同源反代 |
| 代码 | `/opt/aiops-login-proxy/`（venv） | `python -m aiops_diagnostics.dify_login_server` |

## 三条硬前置（不满足就别装）

1. **控制台入口已是 TLS**（#776）。`AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN` 非 https 时代理
   **拒绝启动** —— 公司密码不得经明文传输。
2. **公司侧权限码已存在**（#777），且 `AIOPS_LOGIN_PROXY_COMPANY_CLIENT_TYPE` 与那两个
   权限码所挂的 `client_type` **一致**；否则公司侧取到空角色集，表现为"你没有权限"。
3. **公网已封掉 Dify 原生账号入口**（#783）：`/console/api/login`、找回密码、邮箱验证码、
   注册、`/activate`。不封，公司登录就不是唯一入口。

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
