# Dify 控制台里的「发布到 AI-Ops」按钮（#577 的最后一跳）

**这是什么**：在 Dify 控制台的编排页注入一颗按钮，运营点它就把**当前改好的内容**
冻结成一个已发布的线上版本。**不用 SSH、不用敲命令。**

决定的论证在 **ADR-0021**；后端桥在 `src/aiops_diagnostics/dify_console_publish.py`。
本目录只是**部署资产**。

## M3 部署包与恢复

Gateway 的 CD 不更新本桥。撤回、恢复和版本列表要求 **Python 桥、JS、nginx snippet**
同版；只更新 Gateway 不能交付运营入口。`build-bundle.sh` 从确定 Git 修订取这三部分及
桥的全部标准库依赖，写入 `REVISION`、`SHA256SUMS`，不携带 env 或凭据。

```bash
# 开发机：只用已合并修订，输出目录为私有目录
bash ops/dify-console-bridge/build-bundle.sh origin/main /tmp/bridge.tar.gz
# 使用 dev-host cp 36 ... --dest <暂存路径> --artifact-sha256 <上述摘要> 上传。
# 服务宿主：先验证外层 tar 摘要，再解压到新的暂存目录；通过 dev-host exec 执行：
bash <暂存目录>/deploy.sh <暂存目录> <新的私有备份目录>
```

脚本更新现有服务目录，保留服务身份、解释器、env、登录代理及 TLS 入口。
它会短暂停止桥，替换代码及静态文件，`nginx -t` 后启动桥并 reload nginx；
三个新路由的无会话请求必须得到 403。任何失败自动恢复原包、JS、snippet 并重启桥。
成功后还须用登录运营账号做发布/试用/撤回/恢复验收；无会话 403 只证明路由和拒绝门。

成功部署后若要人工恢复，使用本次备份（不恢复配置或数据库）：

```bash
systemctl stop aiops-dify-console-bridge.service
rm -rf /opt/aiops-console-bridge/aiops_diagnostics
cp -a <备份>/aiops_diagnostics /opt/aiops-console-bridge/aiops_diagnostics
cp -a <备份>/dify-publish-button.js /srv/aiops-console-assets/dify-publish-button.js
cp -a <备份>/dify-console-publish.conf /www/server/panel/vhost/nginx/snippets/dify-console-publish.conf
/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload
systemctl start aiops-dify-console-bridge.service
```

以上是操作方法，不是生产启用授权；部署与运行配置的变更范围需单独写入私有变更单。

## 四件东西，装在哪

| 文件 | 装到 | 作用 |
|---|---|---|
| `dify-console-TLS.conf` | `/www/server/panel/vhost/nginx/` (36) | **TLS 域名入口**（#776）：`dify.ranlei.work` 的 80 跳转 + 443，反代到 10008 |
| `dify-console-10008.conf` | `/www/server/panel/vhost/nginx/` (36) | 10008 server 块：转发 Dify + 注入（TLS 入口的下游，也是回滚路径） |
| `dify-console-publish.conf` | `/www/server/panel/vhost/nginx/snippets/` | 桥的同源转发（含 cookie 与 CSRF 头） |
| `dify-publish-button.js` | `/srv/aiops-console-assets/` | 注入进页面的脚本。**不含任何凭据** |
| `aiops-dify-console-bridge.service` | `/etc/systemd/system/` | 桥的服务单元（身份 `aiopsbridge`） |

## 拓扑

```
浏览器 ──► https://dify.ranlei.work（dify-console-TLS.conf，443）
             └─► 127.0.0.1:10008（dify-console-10008.conf）
                   ├─ /            ──► 127.0.0.1:10018（Dify 自己的 nginx）
                   ├─ /__aiops/publish-button.js  ← 注入脚本从这里出
                   └─ /__aiops/publish            ──► 127.0.0.1:10118（桥）
```

**TLS 那一层反代到 10008，不是 10018。** 注入、发布脚本与桥的三个 location 都在 10008
那一层；指到 10018 会把它们整个绕过。10008 同时是本次变更的**回滚路径**（退役是另一件
有观测依据的单独动作）。详见 `dify-console-TLS.conf` 顶部的注释。

**Dify 的发布端口必须收进回环**：`deploy/docker-compose.yaml` 里
`127.0.0.1:${DIFY_EXPOSE_PORT:-10018}:80`。绑 `0.0.0.0` 会让 10018 成为绕过注入层的
第二条路。

## 四处**容易踩空**的地方（都是实测踩过的）

1. **CSRF 头必须转下去。** Dify 的 `/console/api/*` 除了 cookie 还要
   `X-CSRF-Token`，少了它一律 401 `CSRF token is missing or invalid` ——
   桥会把它误报成"会话失效"。桥和 nginx snippet 两处都要带。
2. **注入脚本不能放在 `vhost/` 目录下。** 那两级目录是 `drw-------`（宝塔私有区），
   nginx 的 worker **走不进去**，取脚本时 403。放 `/srv/aiops-console-assets/`。
3. **`sub_filter` 要求上游不压缩。** 所以 `proxy_set_header Accept-Encoding ""` +
   `gzip off`；少了这两行，替换**静默失败**（看着配了其实没注入）。
   另外脚本里的符号全部带 `aiops` 前缀 —— 第一版直接叫 `publish`/`toast`，
   与 Dify 自己的 bundle 撞名，报 `publish is not a function`。
4. **⚠️ cookie 名随 scheme 变，读 cookie 必须两种前缀都试。** Dify 在
   **https 且 `COOKIE_DOMAIN` 为空**时给控制台 cookie 加 `__Host-` 前缀
   （`api/libs/token.py` 的 `is_secure() and _cookie_domain() is None`），http 下是裸名。
   #776 把控制台收到 https 后名字就从 `csrf_token` 变成了 `__Host-csrf_token`。
   - **桥侧已经兼容**（`console_session_from_headers` 同时识别两种，转发时按实际前缀回填）；
   - **注入的按钮脚本也必须兼容**，否则 `aiopsReadCookie('csrf_token')` 读到空串 →
     提交空 CSRF → 桥 401，而**界面上完全看不出原因**（按钮在、点了就失败）。
   判据：在 https 控制台里 `document.cookie` 应含 `__Host-access_token` / `__Host-csrf_token`。
   设计取舍：**用 `__Host-`，不设 `COOKIE_DOMAIN` 把它关掉** —— 前缀带来的隔离更强，
   代价就是这一类读 cookie 的地方都得跟着改（v2 的登录代理也有同样的待办，见 #776 记录）。

   附带一条同源的实测：**多层 `proxy_pass` 下 `sub_filter` 是逐层独立的**。TLS 那一层
   反代到 10008 时，注入仍由 10008 负责；若将来把注入挪到外层，**必须同时删掉内层那份**，
   否则页面里会出现两个 `/__aiops/publish-button.js`。验收时数它出现几次即可。

## 桥读什么

`/etc/aiops-console-bridge/bridge.env`（**0600 root**）：

```
AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY=…    # 服务端持有；**不进浏览器**
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
| **配了，代理查不通** | **拒**（fail-closed） | **退回固定调试租户** |

第三条两个方向的差别是**有意**的：发布退回旧规则等于放宽**授权**；试运行退回的是
一个已经上线并验收过的既有行为（#781 的第一版），不是新的越权。理由与代价写在
`submit_trial` 的 docstring 与 ADR-0028。

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
