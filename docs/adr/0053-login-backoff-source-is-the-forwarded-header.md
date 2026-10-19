# 53. 登录退避的来源取自 nginx 声明的头，不取自对端地址

日期：2026-10-19
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_login_server.py`（`_client_source()`、失败路径新增
一行结构化结果码日志）

## 背景：一个人输错，全租户 429

登录代理只监听回环，所有浏览器都经宿主 nginx 同源反代进来，因此
`self.client_address[0]` **恒为 `127.0.0.1`**。而退避键正是它：

```python
source = self.client_address[0] if self.client_address else "unknown"
```

「来源」这个维度被抹成了一个常量。2026-10-19 在 41 上实测：用 6 个**不同**用户名连续
失败，第 5 个开始 429（阈值 5/300 秒），之后**所有**登录请求都是 429 —— 包括正在正常
输密码的人。这不是理论风险，是已经发生过的拒绝服务。

同一批里还有第二个问题：失败不留因。成功会打
`dify-login: user=… permissions=…`，失败只有一行访问日志
`"POST /__aiops/login" 502`。八种成因在 journal 里无法区分，排查只能靠回放重建。

## 决策：只读 `X-Real-IP`，缺失才回落对端；且让那一层把值修正对

只读一个头还不够 —— 评审时（Devin, PR #851）指出了同一链路上的**第二跳**：

```
browser ──TLS──> nginx:443  ──> 127.0.0.1:10008 ──> 127.0.0.1:10119
                 X-Real-IP=运营地址         真实配置在这里又覆盖了一次
                 X-Forwarded-Proto=https    $remote_addr 现在是 127.0.0.1
                                            $scheme 现在是 http
```

`dify-console-10008.conf` 的 `location` 里同样写着
`proxy_set_header X-Real-IP $remote_addr`。所以只看后端读哪个头，等于把常量从
`client_address` 换成了另一个常量 —— **https 主入口上「按人隔离」仍然失效**。

修在这一层：用 `realip` 模块在 location 求值**之前**修正 `$remote_addr`，且只对回环
对端（= 本机 TLS 入口）生效。

```nginx
set_real_ip_from 127.0.0.1;
set_real_ip_from ::1;
real_ip_header X-Real-IP;
real_ip_recursive off;
```

于是 snippet 里那行 `$remote_addr` 拿到的是真实运营地址；而**直连明文入口**的客户端
自带 `X-Real-IP` 不被采信（对端不是回环），拿到的仍是它自己的地址。

协议同理但更绕：nginx 没有"条件地覆盖 `$scheme`"，所以用一个 map：

```nginx
map $realip_remote_addr $aiops_upstream_proto {
    default         "";
    ~^127\.0\.0\.1$ $http_x_forwarded_proto;
    ~^::1$          $http_x_forwarded_proto;
}
```

`$realip_remote_addr` 是 realip 处理**之前**的对端地址 —— 用它判"谁在跟我说话"，
而不是用已被修正过的 `$remote_addr`（那会让判断自指）。

**不读 `X-Forwarded-For`**，尽管它也在配置里。区别是覆盖方向：

- `X-Real-IP` 在 nginx 里是**覆盖写** —— 客户端送什么都不影响最终值。
- `X-Forwarded-For` 是**追加**语义（`$proxy_add_x_forwarded_for`），客户端自带的值会
  留在链里。采信它等于给"每次带一个新的 XFF 就绕过退避"开门。

缺失 `X-Real-IP` 时回落到对端地址：那是直连（不经 nginx）的场景，此时对端**就是**真的
来源。不 fail-closed 到"永不退避"，因为那会把限流整个关掉。

失败路径补一行 `WARNING`，只含结果码、HTTP 状态与来源：

```
dify-login: rejected code=COMPANY_LOGIN_REJECTED status=403 source=203.0.113.77
```

不含密码、不含令牌、不含验证码字符。结果码本来就是稳定的、非本地化的
（`DifyLoginError.code`），所以这一行可以直接用来数成因分布。

## 被否掉的替代形态

- **在 nginx 侧把 `X-Real-IP` 换成 `X-Forwarded-For` 单值**。改的是一个已经在三处
  正确工作的配置，只为让后端能继续读对端地址；而"读哪个头"本来就该由服务端自己声明。
- **把退避做成全局计数器**。那不是"修来源判定"，是退掉限流本身：任何一个人都能锁死
  所有运营。现状的缺陷正是它的退化形态。
- **按 `<来源, 账号>` 的哈希做分片避免内存增长**。退避表本来就是进程内、重启即清的
  小字典（注释里已写明这不是计费级限流）；为了一个未观测到的规模问题引入哈希，是拿
  正确性换一个不存在的问题。
- **只在日志里补来源，不改退避键**。那样这次事故仍然会复现，只是下次查得快一点。
- **只改后端的读头逻辑，不动 nginx**（初版就是这样，评审时被指出）。第二跳会把
  `$remote_addr` 再覆盖一次，后端读到的仍是 `127.0.0.1` —— 修了个寂寞。
- **让后端去读 `X-Forwarded-For` 的最后一个值**。那需要后端理解跳数，而跳数由部署
  拓扑决定：多一层反代就错一位，少一层就越界。nginx 侧的 `realip` 已经把这个判断
  做对了（只信 `set_real_ip_from` 列出的对端），后端不该重复实现一遍。

## 后果与代价

- **正向**：一个人输错不再影响别人；同一来源、同一账号的退避都仍然生效（两条各自
  有用例钉住，含"换 IP 不能换来无限次猜同一账号"）。
- **代价一**：nginx 配置里多了一段 realip 与一个 map。它们只在 `10008` 那一层，
  而且是**幂等**的：重复修正同一个值不会改变结果。
- **代价**：信任一个 HTTP 头。这个信任的前提取自**部署拓扑**——它只监听回环，回环上
  只有宿主 nginx。若哪天有人直连 10119（不经 nginx），`X-Real-IP` 就变成调用方可控的
  值，退避可被绕过。这一点写进了 `_Backoff` 的 docstring，是部署约束，不是代码能保证的。
- **未覆盖**：真实浏览器经 `https://dify.ranlei.work` 的端到端复验，要等部署之后。
