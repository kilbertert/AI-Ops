# 环境工具HTTPS扩展入口

本片承接[开通预检](../../../docs/agents/environment-onboarding-preflight.md)的共享执行侧→目标环境方向。
使用公司既有HTTPS虚拟主机扩展目录；不改公司代码、不新建端口、不建立业务SSH隧道。
该片段只开放现役工具回调，运行凭据及租户权限仍由Gateway验证；不是用户聊天或管理页入口。

## 生成固定制品

先只读核对已有域名、TLS证书、server的include位置、`/v1/internal/`旧路径及实际访问，
确认没有现用路径被遮蔽。目标必须有Real-IP模块，且来源是实际连接方；前置代理场景不能
把伪造的X-Forwarded-For当来源，也不能仅把公共代理IP加入白名单。
核HTTP/server/default-server的错误日志级别不得为`info`或`debug`，且没有debug_connection；
请求头解析发生在location选择前，重复Authorization等畸形头可被这些级别记录，片段无法屏蔽。
不符合时拒绝安装，另行评审日志配置，不能未经授权关闭公司日志。目标只读检查与原始摘要留私有记录。

```bash
bash ops/environments/gateway/build-ingress.sh <已合并SHA> /private/ingress.tar.gz \
  http://127.0.0.1:<接入模块端口> <共享执行侧IPv4> <共享Dify侧IPv4>
```

在开发机运行，包内只有`REVISION`、`SHA256SUMS`和生成片段；来源地址是私有部署参数，
制品及其摘要留私有运维账本。源代码/工具声明来自同一合并修订，不能用未合并工作树生成生产配置。
生成器拒绝远端上游、网段、重复来源和指令注入；可用`render_node_callback.py --ingress`
在隔离本机测试，其他模式的既有节点渲染接口保持不变。

## 安装与恢复边界

安装前单独取得目标虚拟主机扩展路由与Nginx reload授权，记录来源、固定回环端口、
新文件的唯一名称、现役配置摘要、备份位置及实际流量观察。以下是固定步骤，不授权任意改动：

1. 通过`dev-host cp --artifact-sha256`上传已合并制品；核外层和包内摘要，拒绝未知或不匹配文件。
2. 备份本次目标片段（初次不存在也记账），先`nginx -t`确认原配置正常。
3. 仅安装本次扩展文件，保持root所有且不可被组/其他用户写入，再`nginx -t`。
   语法失败恢复原文件，不reload；成功才reload并读取实际响应与进程状态。
4. 核原公司API基线，分别从实际共享调用侧验证TLS、匿名拒绝、授权工具成功；
   非授权来源、明文HTTP、未知工具、错误方法应拒绝，伪造转发头不得放行。
5. 任一步失败恢复本次片段，语法校验后reload并复验原入口；不停止公司业务服务、不删除共享配置。

这不是通用安装器已交付；未通过目标验证不激活目录。配置保留固定路径白名单，
请求头只传运行Bearer、Content-Type及现有执行/工具合同头；片段不记录带订单参数的URL或上游错误正文。
此边界依赖上述宿主日志前置条件，不承诺任意宿主解析阶段日志都被片段覆盖。
前置核查区分真实服务凭据与宿主其他日志字段：工具调用只携Bearer，不携浏览器Cookie；
宿主格式若包含Authorization则拒绝安装。存在Cookie日志时不得将本入口复用为浏览器会话代理，
不能把服务Bearer验收外推到浏览器凭据；不要未经授权修改公司既有日志。
例如TRACE在选择location前被Nginx拒绝，仍可能进入宿主原access log；任何调用均不得把
Bearer或其他秘密放在URL。需要连畸形请求URL也不记录的环境，应另行审批虚拟主机日志策略，
不能宣称此扩展片段完成了宿主级日志脱敏。
新增工具后从同一工具声明重新构建片段，禁止手工加宽到任意`/v1/*`。

## 验证

```bash
uv run pytest tests/integrations/test_environment_tool_ingress.py tests/test_node_runtime_deploy.py
```

真实Nginx与测试证书覆盖原公司路径、声明工具、身份头隔离、匿名拒绝、错误方法、未知路径、
伪造Real-IP及HTTP拒绝。最初仅在父location写来源if，被子路径的rewrite指令遮蔽；
检查现位于每个允许路径，保留原反例。测试上游是替身，不能证明真实公司数据或业务结论。

## 环境用户助手入口

同一台宿主、同一个公司既有 HTTPS vhost 上的第二类扩展片：只装回调片时正常用户
`/v1/` 一律 404（客户端 FAQ/问答/会话从未到过本环境 Gateway）。本片在同一 server 块
补开**用户助手精确入口**，与回调片互不修改、各自独立回退。信任边界见 ADR-0319。

### 开放面与凭据分流

转发面唯一声明处是 `ops/nginx/environment-user-ingress-routes.txt`：FAQ 三件套、
`POST /v1/assistant/questions`、问答详情与取消、`/v1/media/{signed_id}` 签名媒体读取、
`/v1/conversations` 的读取/新建/删除。
管理面（`/v1/dify/*`、`/v1/agents`、`/v1/operator-*`）、`/v1/internal/*` 不进本片，
客户端也拿不到任意 URL——上游固定本机 `127.0.0.1:8788`（本环境登记端口）。

入口分流在 server 上下文 `set`/`if` 完成（不新增 `map`，location 内 `if` 只有
`return`，无写语句），依据是客户端 `X-Business-Entry`——它只决定用哪套凭据
处理，不是身份本身。入口值按上游同款规范化（去空白+小写）识别，缺失按
consumer 处理（41 公开入口实测契约：缺头旧客户端=consumer 链）：

| 申报入口 | Authorization | 会话头 | X-AIOps-Source-Key |
|---|---|---|---|
| `consumer` 或缺失 | 换成本环境服务 Bearer（凭据片值） | `third-session`→`X-Third-Session` | 清空（走会话解析链） |
| `operator`（规范化后） | 透传个人 Bearer | 两种会话头都清空（不混 consumer 会话） | 本环境独立值（走公司令牌链，凭据不与其他环境共享） |
| 其他非空值 | 不转发 | — | location 内 403 |

客户端自报的 `X-Third-Session`、`X-AIOps-Source-Key`、`X-AIOps-Environment(-Key)`、
`X-AIOps-Gateway-Assertion`、`Cookie` 一律覆盖或清空，到不了上游（95 宿主
access_log 记 Cookie 字段，身份走自定义头不走 Cookie）。身份仍由 Gateway 全量验证；
环境目录停用该环境时 Gateway 继续拒绝——本片不授予、不切换任何路由。
operator 走公司令牌链是本环境的真实 resolver 需要来源密钥分派（`SourceKeyCallerResolver`
把密钥验过的请求交给 `CompanyTokenCallerResolver`，即公司体系身份契约 D5）；
环境未配来源门时凭据片留空值，operator 请求落到既有链被拒——这是 fail closed，不是放行。
**本片只是头/schema 门**：95 当前配置没有 `company_source_key`/公司 JWT，普通
operator 请求在 fallback 会话链上仍被拒——operator 正式可用以 root 单独确认的
来源认证配置为准，本片不迁移旧 payload-trust 路径、不改公司配置或运行时。

`/v1/media/` 例外：**签名即授权**，不看 `X-Business-Entry`、不透传任何身份头——
该 location 内联 `proxy_pass`，Authorization/入口/会话/来源/环境断言/Cookie 全清空，
只认 `media_<32>.<24>` 形状与 Gateway 签名校验（上游实测无 Authorization 即服务字节）。

本片全部 location 的 `access_log` 逐条关闭：签名 media URL 与带 ID 的会话/问答
URI 本身就是凭据载体，宿主 access_log 会原样记 URI——旧 internal 片在自有 server
块 server 级关，本片在公司 server 内只能逐 location 关，**只关本片**，公司其他
location 日志保持原样；error_log 按旧 internal 模式不在片内配置。
这是 URI 凭据的日志豁免，不声称宿主日志隐私整体已解决：错误日志级别与
其他字段仍按前置核查拒绝安装。

### 生成固定制品

```bash
bash ops/environments/gateway/build-user-ingress.sh <已合并SHA> /private/user-ingress.tar.gz <环境号>
```

在开发机运行，包内只有 `REVISION`、`SHA256SUMS`、渲染环境号后的 conf/inc 与路由声明；
**不含任何凭据值**。源代码/片段声明来自同一合并修订，不能用未合并工作树生成生产配置。
制品及其摘要留私有运维账本，环境号只决定 `/etc/aiops-access-<环境号>/` 受控路径。

### 凭据片（root 现场生成，0600，不入制品）

安装机创建 `/etc/aiops-access-<环境号>/nginx-user-ingress-auth.conf`，0600、属主同服务账号，
内容仅两行 `set`（无其他指令）：

```nginx
set $aiops_user_service_authorization "Bearer <本环境服务凭据>";
set $aiops_user_operator_source_key "<本环境来源密钥，未配来源门则空串>";
```

服务凭据与环境 Gateway `third_session_service_token` 同源同值，来源密钥与该环境
Gateway `company_source_key` 同源同值——两处值都只从运行时受控位置取，
不进仓库、制品默认值或日志；**不复用其他环境的同名凭据**。
缺失此文件 `nginx -t` 直接失败（未知变量），即不存在"没带凭据先放行"的窗口。

### 安装前置、验证与回退

前置核查与上方工具入口同一套纪律：只读核对 vhost include 位置、没有
`location ^~ /v1/` 或更长 `^~` 前缀遮蔽本片路径、错误日志级别非 info/debug、
access_log 格式不含 Authorization/Cookie 等秘密字段、无 debug_connection。
本片在共享公司 server 内——**不得**用 `access_log off` 或改写宿主日志来"达标"，
格式不合即拒绝安装另行评审，不未经授权动公司日志。

1. `dev-host cp --artifact-sha256` 上传制品；核外层与包内摘要。
2. 备份目标位置（初次不存在也记账）；先 `nginx -t` 确认原配置正常。
3. 安装 conf/inc/凭据片（凭据 0600，conf/inc root 所有不可组写），再 `nginx -t`；
   失败恢复原文件不 reload，成功才 reload。
4. 用**本环境**正常会话/JWT 验收：consumer 会话问题到本环境数据、operator Bearer
   走通、非法入口 403、未声明方法 405、管理面与 `/v1/internal/*` 404；
   环境目录停用侧仍拒绝。不能用匿名 health 代替真实身份验收。
5. 回退按安装前备份形状分两种：初装仅移除本次 conf/inc/凭据片再 `nginx -t`+reload；
   覆盖安装则恢复备份中的旧版三件文件再校验 reload。均不改回调片、不动公司其他
   配置、不停止公司业务。

本地验证：`uv run pytest tests/operations/test_environment_user_ingress.py`——两层：
真 nginx + 回显上游覆盖声明路由全转发、consumer（含缺头）凭据注入与会话转换、
operator 规范化透传与伪造头/Cookie 清空、非法入口/错误方法拒绝、管理面与声明外
`/v1/` 路由不转发；真 nginx + **真 Gateway 进程**（一次性数据根，verify-aiops-gateway
接缝层）覆盖匿名真实 401、合成会话真解析、operator 真实 schema 替身经来源密钥分派、
登记目录停用真实 403、签名媒体真出字节、伪造来源密钥换不到公司身份。
回显层上游是替身、seam 层 resolver 是契约形状替身，均不证明真实公司数据或业务结论。
