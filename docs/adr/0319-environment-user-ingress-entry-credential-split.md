# 319. 环境用户助手入口按申报入口分流凭据，客户端自报身份头一律到不了上游

状态：已采纳；为环境既有 HTTPS vhost 增加用户助手精确入口（`ops/nginx/environment-user-ingress.conf`
+ `environment-user-ingress-proxy.inc` + `build-user-ingress.sh`）。

新业务环境的既有 HTTPS 域名只装了 `/v1/internal/` 回调精确片：正常用户客户端的
FAQ/问答/会话请求从未到过本环境 Gateway（整片 `/v1/` 404）。要在**同一公司 vhost**
补开用户入口，必须回答两个问题：转哪些路径、凭什么决定用哪套凭据。

环境上有两条身份链：`consumer` 会话链（`third_session_service_token` + thirdSession
→ Redis 解析，即本环境服务凭据换会话身份）与 `operator` 公司令牌链（个人 Bearer 经
`SourceKeyCallerResolver` 的来源密钥门交给 `CompanyTokenCallerResolver`，即
公司体系身份契约 D5）。会话链被配置后 fallback 只剩 `RedisThirdSessionResolver`——
operator 的个人 Bearer 过不了服务凭据比对，**没有来源密钥注入就走不通**；
consumer 反过来必须看不到来源密钥（密钥验过会把它错分派到公司令牌链）。

## 决定

- 路径是精确白名单（`environment-user-ingress-routes.txt` 唯一声明）：FAQ 三件套、
  `POST /v1/assistant/questions`、问答详情、`/v1/conversations` 的读取/新建/删除。
  管理面、agent 注册、`/v1/internal/*` 不进本片。`=`/`^~` + `$uri` 形状闸，
  不依赖宿主原 location 写法、不被 `^~ /v1/` 遮蔽；测试断言网关其余 `/v1/` 路由不进。
- 入口分流在 server 上下文 `set`/`if` 完成（不新增 `map`，location 内 `if` 只 `return`）：
  `X-Business-Entry: consumer` 换环境服务 Bearer 并转换会话头；`operator` 透传
  Bearer、清两种会话头、注入本环境来源密钥；其他/缺失入口 403。
  入口头只选凭据处理方式，身份仍由 Gateway 全量验证；环境目录停用照常拒绝。
- 凭据片（服务 Bearer + operator 来源密钥）由 root 现场 0600 生成在
  `/etc/aiops-<环境>/`，制品只带渲染环境号的模板——不进仓库、构建默认或日志；
  缺失凭据片 `nginx -t` 失败，没有"先放行"窗口。各环境凭据独立，不共享。
- 客户端自报的 `X-Third-Session`、原始 `third-session`、`X-AIOps-Source-Key`、
  `X-AIOps-Environment(-Key)`、`X-AIOps-Gateway-Assertion` 一律覆盖或清空。

## 替代方案

`map` 做入口→凭据组映射（41 现网用法）：需要在 http 上下文新增全局 map，本片要求
不新增全局状态、只用 server 上下文 `set`/`if`，且 map 的失败默认位写法在多环境复用时
更容易写松，排除。
按 `third-session` 头有无分流：operator 请求残留会话头会错走 consumer 链（正是不许
"混会话"要防的形状），缺失会话的 consumer 请求又会静默落到 operator 链，排除。
整片 `location ~ ^/v1/` 或 `location /v1/` 前缀转发：把管理面、内部回调一起敞到公网
域名，暴露面变更无观测依据，排除。
operator 也走公司网关那一跳（41 的 30899 链）：本环境没有可复用的公司网关前置，
且会话链已占 fallback——来源密钥注入是本环境 resolver 的实际需要，排除。
凭据写入模板默认值或构建参数：凭据会进制品/仓库，与"运行时受控位置"约束冲突，排除。

## 后果

- 用户入口与回调片在同一 vhost 共存、各自独立安装与回退；本片回退不动公司配置。
- `X-Business-Entry` 成为本环境客户端必须发送的契约头（与现有客户端一致）；
  未发送或非白名单值收到 403 而不是被默认成某入口——不引入静默归类。
- operator 可用性依赖本环境 `company_source_key` + 公司令牌验证已配置；
  未配置时凭据片留空、operator 请求被既有链拒绝——这是设计内的 fail closed。
- 宿主日志前置仍是边界：access_log 含 Authorization 或错误日志 info/debug 时拒绝安装，
  本片不声称覆盖了宿主解析阶段的日志形态。
- 共享 vhost 内无法在本片内关闭 access_log（那会动公司日志）——凭据不落 URL、
  Bearer 不进查询串是安装方与调用方共同守的边界。
