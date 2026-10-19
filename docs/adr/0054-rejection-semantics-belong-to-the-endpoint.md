# 54. 公司网关的两个端点各有自己的拒绝语义，映射按端点声明

日期：2026-10-19
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_login_company.py`（`_mapping(login=…)`、新增
`COMPANY_CAPTCHA_REJECTED_STATUS`、`_reason_from_message`）、
`src/aiops_diagnostics/dify_login_server.py`（`_public_reason` 不再抹平已知成因）

## 背景：一次验证码输错，被告知「公司登录服务暂时不可用」

运营在登录页输错验证码，截图里出现的是：

```
公司登录服务暂时不可用，请稍后重试
```

而公司侧的回包一直是：

```
HTTP 428  {"code":1,"msg":"验证码错误","data":null,"ok":false}
```

两者之间是一条**分类链**，每一环单独看都合理：

1. `bounded_http.AUTH_REJECTED_STATUSES = {401, 403}` —— 通用 Web 惯例，对标准上游正确。
2. 428 不在其中 ⇒ `classify_http_error` 归入 `http_error`。
3. 该客户端的 `http_error` 映射是 `CompanyUnreachable(COMPANY_UNAVAILABLE)`。
4. `_public_reason` 把 `CompanyUnreachable` 说成「服务暂时不可用」。

**凭据类事实在响应体里，分类器只看状态行。** 这个上游把语义放在 `msg`（三个验证码分支
都是 428：`验证码不能为空` / `验证码失效` / `验证码错误`），而骨架按状态码分流。

2026-10-19 在 41 部署的代理上复现：新 `randomStr` 取图 200 → 故意填错 code →
`502`（0.068 秒，没有在等超时）；同一秒直连上游同一错码 → `428 验证码错误`；
故障时刻前后各一次取图都是 200 —— **上游一直活着**。

### 为什么测试绿着

假公司替身的「验证码不能为空」分支回的是 **HTTP 200**，真网关回 **428**。
替身把状态码写软了 ⇒ 分类器走不到 428 ⇒ 误分类永远不出现。

这个文件里连着两处注释都在讲同一类事故（替身早期按 body 解析、替身早期回 `results`
而非 `invitation_results`），这是第三次。

## 决策：映射按端点声明，不拓宽全局默认

`_mapping(*, login: bool)` 分两份：

| 端点 | 凭据拒绝 | 拒绝状态码 |
|---|---|---|
| `/upms/token/login` | `CompanyLoginRejected`（运营的输入问题） | `{401, 403, 428}` |
| `/upms/user/info` | `CompanyUnreachable`（我们自己令牌的问题） | `{401, 403}` |

理由不是"428 也是个拒绝"，而是**这两类在业务语义上本来就不同**：

- 登录端点的拒绝是「你输的东西不对」，回给运营，他可以自己改。
- 读权限码端点的拒绝是「我们刚拿到的那枚令牌不被接受」，运营改了也没用。

`ErrorMapping.auth_rejected_statuses` 本来就是**按客户端声明**的扩展点
（`caller_auth` 已经用了 `{400,401,403}`），所以这不是新机制，是把已有的扩展点用在该用的地方。

拒因文本复用同一条分流函数 `_reason_from_message`，并**把验证码判定提到密码之前** ——
公司的验证码过滤器先于密码校验，两者不会同时命中；若哪天报文同时出现，那是验证码这一关没过。

`_public_reason` 对 `CompanyLoginRejected` 改为**直接回它自己的 message**。原先在这里
再套一句「公司账号或密码不正确，或验证码无效」，等于把 `_reason_from_message` 刚分好的
成因重新抹平 —— 运营分不清是密码打错还是验证码过期。message 永远是本仓库的固定字符串，
不含上游原文，因此这条放宽没有泄漏面。

## 被否掉的替代形态

- **把 428 加进全局 `AUTH_REJECTED_STATUSES`**。影响面是全仓出站客户端：428 的标准含义是
  `Precondition Required`，一个正常的 REST 上游用它表达"你的请求缺少前置条件"，把它
  一律读成"凭据被拒"会让别的客户端开始误报。而且全局表改变不了**文案**，只改变分类 ——
  运营看到的东西还是错的。
- **让 `classify_http_error` 去读响应体 `code`**。骨架是纯函数、不认识任何上游的信封，
  把信封知识塞进去就把"信封解析归调用点"这条边界拆了；而这个上游真正的信封是**扁平的**
  （`_raw_envelope`），连 `{code,data}` 都不是。
- **在 `dify_login_server` 里按 `exc.code` 字符串分支**。会把业务分类知识从客户端挪到
  HTTP 层，而且第一层（`execute` 之前）就已经丢了状态码，只能靠 message 匹配。
- **只在 server 层把 `COMPANY_UNAVAILABLE` 改个文案**。治症状：分类仍然是错的，日志、
  审计、退避都还在按"上游故障"处理。

## 后果与代价

- **正向**：运营看到的每一句话都能据以行动。
- **代价**：一个客户端里现在有两份 `ErrorMapping`，读代码的人要理解"为什么不是一份"。
  这一段注释就写在 `_mapping` 的 docstring 里。
- **未覆盖**：真实浏览器里以真实账号输错一次验证码的端到端复验（本票的单元判据只覆盖到
  代理的 HTTP 面）。这与 #783 里仍未验的「人在页面上点得动」是同一类欠账。
