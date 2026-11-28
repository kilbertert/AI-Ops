# Dify 控制台里的「发布到 AI-Ops」按钮（#577 的最后一跳）

> **标准 ACR 发布与 36 总览**：本服务现已收敛至 `aiops-dify-edge` OCI 镜像（与登录代理共享构建镜像、双容器隔离运行，见 [`deploy/dify-36/ARCHITECTURE.md`](../../deploy/dify-36/ARCHITECTURE.md) 与 ADR-0367）；以下 `build-bundle.sh` / `deploy.sh` 为兼容保留的历史 tar 回滚细节。

受限Dify服务API入口、正式用途及合并制品安装与恢复见[操作手册](../../docs/agents/shared-dify-https-fallback.md)。
它不提供控制台或内部管理服务；是否已部署以交付回执为准。

**这是什么**：为 Dify 控制台提供同源 `/__aiops/*` HTTP 发布桥服务（`publish`/`retract`/`restore`/`versions`/`trial`/`operator-preview`/`manage`/`knowledge-open`）。前端界面与按钮组件统一位于 [`deploy/dify-36/web-patches/`](../../deploy/dify-36/web-patches/README.md)，不再使用 Nginx `sub_filter` 运行时 JS 注入（ADR-0366）。

决定的论证在 **ADR-0021**、**ADR-0338** 与 **ADR-0366**；后端桥独立位于 `services/dify-console-bridge/dify_console_bridge/`。
本目录只是**部署资产**。

管家原生预览与受管发布条已直编进 `deploy/dify-36/web-patches/`（无需 Nginx 注入 JS）。更新后的 Python 桥及 `/__aiops/operator-preview` 路由需与前端镜像保持兼容；其 SSE 禁止代理缓冲，200秒代理超时覆盖180秒预览上限。详见[原生预览手册](../../docs/agents/operator-cpms/native-preview.md)。

## M3 部署包与恢复

Gateway 的 CD 不更新本桥。撤回、恢复和版本列表要求 **Python 桥与 nginx snippet** 同版。`build-bundle.sh` 从确定 Git 修订打包这两部分及桥的全部标准库依赖，写入 `REVISION`、`SHA256SUMS`，不携带 env 或凭据。

```bash
# 开发机：只用已合并修订，输出目录为私有目录
bash ops/dify-console-bridge/build-bundle.sh origin/main /tmp/bridge.tar.gz
# 使用 dev-host cp 36 ... --dest <暂存路径> --artifact-sha256 <上述摘要> 上传。
# 服务宿主：先验证外层 tar 摘要，再解压到新的暂存目录；通过 dev-host exec 执行：
bash <暂存目录>/deploy.sh <暂存目录> <新的私有备份目录>
```

脚本更新现有服务目录，保留服务身份、解释器、env、登录代理及 TLS 入口。
它会短暂停止桥，替换代码及静态文件，`nginx -t` 后启动桥并 reload nginx；
三个新路由的无会话请求必须得到 JSON 403；reload 是异步的，每条路由最多等待 15 次。
超时或安装失败自动恢复原包、JS、snippet 并重启桥。
成功后还须用登录运营账号做发布/试用/撤回/恢复验收；无会话 403 只证明路由和拒绝门。

成功部署后若要人工恢复，使用本次备份（与 `deploy.sh` 自动恢复步骤一致，同步还原两个代码目录与 `bridge.service` 单元，不恢复配置或数据库）：

```bash
systemctl stop aiops-dify-console-bridge.service
rm -rf /opt/aiops-console-bridge/aiops_diagnostics /opt/aiops-console-bridge/dify_console_bridge
cp -a <备份>/aiops_diagnostics /opt/aiops-console-bridge/aiops_diagnostics
if [ -e <备份>/dify_console_bridge ]; then cp -a <备份>/dify_console_bridge /opt/aiops-console-bridge/dify_console_bridge; fi
cp -a <备份>/dify-console-publish.conf /www/server/panel/vhost/nginx/snippets/dify-console-publish.conf
cp -a <备份>/bridge.service /etc/systemd/system/aiops-dify-console-bridge.service
systemctl daemon-reload
/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload
systemctl start aiops-dify-console-bridge.service
```

以上是操作方法，不是生产启用授权；部署与运行配置的变更范围需单独写入私有变更单。

## 组件与安装位置

| 文件 | 装到 | 作用 |
|---|---|---|
| `dify-console-TLS.conf` | `/www/server/panel/vhost/nginx/` (36) | **TLS 域名入口**（#776）：`dify.ranlei.work` 的 80 跳转 + 443，反代到 10008 |
| `dify-console-10008.conf` | `/www/server/panel/vhost/nginx/` (36) | 10008 server 块：转发 Dify + 挂载桥与登录代理 snippet |
| `dify-console-publish.conf` | `/www/server/panel/vhost/nginx/snippets/` | 桥的同源 `/__aiops/*` 转发（含 cookie 与 CSRF 头） |
| `aiops-dify-console-bridge.service` | `/etc/systemd/system/` | 桥的服务单元（身份 `aiopsbridge`） |

## 拓扑

```
浏览器 ──► https://dify.ranlei.work（dify-console-TLS.conf，443）
             └─► 127.0.0.1:10008（dify-console-10008.conf）
                   ├─ /            ──► 127.0.0.1:10018（Dify 自己的 nginx / web 含直编扩展组件）
                   └─ /__aiops/*   ──► 127.0.0.1:10118（桥）
```

**TLS 那一层反代到 10008，不是 10018。** 登录收口与发布桥的 `/__aiops/*` 转发都在 10008 那一层；指到 10018 会把它们整个绕过。10008 同时是本次变更的**回滚路径**。

**Dify 的发布端口必须收进回环**：`deploy/docker-compose.yaml` 里 `127.0.0.1:${DIFY_EXPOSE_PORT:-10018}:80`。

## 关键注意点

1. **CSRF 头必须转下去。** Dify 的 `/console/api/*` 除了 cookie 还要 `X-CSRF-Token`，少了它一律 401 `CSRF token is missing or invalid`。桥和 nginx snippet 两处都要带。
2. **⚠️ cookie 名随 scheme 变，读 cookie 必须两种前缀都试。** Dify 在 **https 且 `COOKIE_DOMAIN` 为空**时给控制台 cookie 加 `__Host-` 前缀（`api/libs/token.py`）。前端直编组件（如 `aiops-knowledge-open.tsx`）与桥侧 `console_session_from_headers` 均同时兼容 `__Host-csrf_token` 与 `csrf_token`。

## 桥读什么

`/etc/aiops-console-bridge/bridge.env`（**0600 root**）：

```
AIOPS_CONSOLE_BRIDGE_PUBLISH_API_KEY=…  # 服务端持有；**不进浏览器**（旧键 AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY 仅过渡兜底，若需回滚旧版本桥，bridge.env 仍须保留旧键）
AIOPS_CONSOLE_BRIDGE_TRIAL_API_KEY=…    # 可选；旧键 AIOPS_GATEWAY_DIFY_TRIAL_API_KEY 仅过渡兜底
AIOPS_CONSOLE_BRIDGE_DIFY_URL=http://127.0.0.1:10018
AIOPS_CONSOLE_BRIDGE_GATEWAY_URL=https://api.mall.qushiyun.com
AIOPS_CONSOLE_BRIDGE_HOST=127.0.0.1
AIOPS_CONSOLE_BRIDGE_PORT=10118
AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN=<浏览器实际访问的控制台 origin>
AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID=<**迁移期**的钉住工作区 ID>
# 配了这一行 ⇒ 桥按**发起人所在租户**解析 workspace（#884），上面那个常量不再参与判定。
# 留空 ⇒ 迁移期行为：仍按 `WORKSPACE_ID` 判，逐字不变。
AIOPS_CONSOLE_BRIDGE_GATEWAY_INTERNAL_TOKEN=
AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES=owner,admin
# 留空 ⇒ **过渡行为**（按 Dify 的 owner/admin 判）。填了 ⇒ 放行判据改为
# 「登录代理说这个会话对应的公司用户持有发布权限码」（#780）。
AIOPS_CONSOLE_BRIDGE_LOGIN_PROXY_URL=http://127.0.0.1:10119
```

**`AIOPS_CONSOLE_BRIDGE_LOGIN_PROXY_URL` 的三种含义要分清**（#780 / #782 / ADR-0028）：

它是**公司身份的来源开关** —— 一个键同时决定三件事（发布权、试运行的订单范围、
审计发起人），所以"半开"不可能发生：

| 状态 | 发布权 | 试运行的订单范围 |
|---|---|---|
| **留空** | Dify owner/admin（过渡行为） | 固定调试租户 |
| **配了，代理可达** | 公司 `aiops:agent:publish` 权限码 | **发起人的公司身份** |
| **配了，代理查不通** | **拒**（fail-closed） | **拒**（共享HTTP入口不能确认环境，不回落默认Gateway） |

共享HTTP入口依ADR0276统一拒绝代理不可达，避免无法确认环境时使用默认Gateway。
`submit_trial` 独立调用仍保留ADR0028的旧调试回落；它不是当前HTTP入口的故障合同。

### 原来的两种含义（保留，仍未过时）

**`AIOPS_CONSOLE_BRIDGE_LOGIN_PROXY_URL` 的两种含义要分清**（#780）：

- **留空**：维持现状 —— 按固定工作区内的 owner/admin 放行。身份闭环上线前就是这个状态。
- **填了但代理不可达**：**拒绝发布**（fail-closed）。不退回 owner/admin —— 那会让
  "核对不通"悄悄变成"按旧规则放行"，而两者是完全不同的事。

配上之后，桥把运营的 **Dify 会话**（也就是登录代理签出的那枚）交给代理核对，代理回
「能不能发布」与审计用的发起人；桥把发起人转成 `X-AIOps-Initiator*` 头交给网关入账。

**为什么桥走 `/v1/dify/publish`（共享密钥）而不是 `/v1/operator/publish`**：
后者认的是「运营的**网关**会话」，而桥手上只有运营的**Dify** 会话 —— 两者换不通。
桥的形态是 BFF：**先把“人”验明白，再以“机器”的身份把事做完**。校验次序为：

1. 浏览器请求的 Origin 与配置的控制台 origin 一致，Cookie 与 CSRF 完整且相符。
2. Dify 返回的工作区 ID 必须等于桥固定的工作区 ID；该 ID 必须与网关 DSL 导出的工作区配置一致。
3. 该工作区中的角色默认只能是 owner/admin；能查看 app 不代表可以发布。
4. Dify 在该工作区找到对应 app，随后网关确认它是已登记绑定的 app，才执行发布。

Referer 只是请求上下文，用户能自行构造，不能代替授权。机器发布密钥覆盖哪些绑定由网关决定；桥没有公司租户级发布身份，不能宣称它继承了 `/v1/operator/publish` 的用户范围。权限默认限于固定工作区的 owner/admin，细粒度发布授权仍需另行设计。

上游重定向不跟随；非预期结果、`refused`/`failed` 均不得报告发布成功。两条门最终复用同一个 `_publish_bound_app`，但凭据与授权模型不同。

## 失败的方向

**注入不上 = 没有那颗按钮，控制台照常。** 挂靠靠 DOM 选择器，Dify 改版可能失效 ——
那时按钮消失，但 Dify 本身不受影响，发布仍可用 `/v1/dify/publish` 或 CLI。

## 包路径迁移

桥构建制品包含独立服务包 `dify_console_bridge/` 与最小标准库原语子集 `aiops_diagnostics/`。包内 `deploy.sh` 将现役 `ExecStart` 的模块段
从 `aiops_diagnostics.dify_console_publish` 或 `aiops_diagnostics.dify.dify_console_publish` 统一改为 `dify_console_bridge`，
不覆盖 User/Group 和环境设置；代码与单元一起备份，失败一起恢复并 daemon-reload。
未升级的旧桥包与旧单元继续配套运行，Gateway CD不替它切换。

共享多环境模式的凭据形状、成套启用与恢复步骤见[登录代理手册](../../docs/agents/dify-login-proxy-36-runbook.md#共享入口选择环境adr0276)。仅配置 `AIOPS_CONSOLE_BRIDGE_ENVIRONMENT_CREDENTIALS_FILE` 并不能开通环境；它还依赖同版本活动登录响应与目标真实工作区。
