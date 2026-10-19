# 36 上部署登录代理（可复跑的手顺）

> **读者**：要在 36 上更新 `aiops-dify-login-proxy` 或它那两个 nginx 配置的人。
> **状态**：2026-10-19 首次成文（#849 上线时实操并踩坑）。
> 决定的论证在 ADR-0023 / 0052 / 0053 / 0055；本页只讲**怎么做、怎么判、怎么回滚**。

## 0. 三句话

1. **代码是手工拷贝**，不是 CD。`/opt/aiops-login-proxy/` 不在仓库部署管线里。
2. **解包后必须核对属主** —— `tar -xzf` 会把归档里的属主/权限一起带过去，而服务身份是
   `aiopslogin`。这一步漏掉的表现是 `No module named aiops_diagnostics.dify_login_server`
   （**报错说模块不存在，实际是读不到文件**）。
3. **判断入口是否生效看端口与 `nginx -T`，不看 `systemctl is-active nginx`** ——
   那个单元从 2026-09-11 起就是 failed（既有债，报 "already running"）。

## 1. 取制品

从一个**已合并的 main 提交**打包，不要从工作区打：

```bash
cd /home/claude/Projects/AI-Ops || exit 1        # 本仓在该机上的实际检出路径；失败就停
pwd; git remote get-url origin                   # 确认是这个仓，不是别的 checkout
git fetch origin --quiet
git rev-parse origin/main                        # 记下这个 sha，验收要用
rm -rf /tmp/stage && mkdir -p /tmp/stage         # ← 少了这行，下一步立即失败
git archive origin/main src/aiops_diagnostics | tar -x -C /tmp/stage
tar -czf /tmp/aiops-login-sync.tgz -C /tmp/stage/src aiops_diagnostics
sha256sum /tmp/aiops-login-sync.tgz              # 传完在远端再核一次
```

## 2. 备份（必须先做）

```bash
ssh yidong-36 'cp -a /opt/aiops-login-proxy /opt/aiops-login-proxy.bak-$(date +%Y%m%d%H%M%S)'
```

nginx 两个文件也备份，**文件名带时间戳**：

```bash
ssh yidong-36 'ts=$(date +%Y%m%d%H%M%S);
  cp /www/server/panel/vhost/nginx/dify-console-10008.conf \
     /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-$ts;
  cp /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf \
     /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-$ts'
```

## 3. 传 + 解包 + **修属主**（这一条最容易漏）

```bash
scp /tmp/aiops-login-sync.tgz yidong-36:/tmp/
ssh yidong-36 'sha256sum /tmp/aiops-login-sync.tgz'      # 与本地比
ssh yidong-36 'cd /opt/aiops-login-proxy && tar -xzf /tmp/aiops-login-sync.tgz
  chown -R root:root aiops_diagnostics
  chmod -R a+rX aiops_diagnostics
  chmod -R go-w aiops_diagnostics
  find . -name __pycache__ -type d -exec rm -rf {} +'
```

**判据**：解包后 `ls -ld /opt/aiops-login-proxy/aiops_diagnostics` 必须是
`root root` 且 `a+rX`（`aiopslogin` 要能读）。不是就先修再重启。

## 4. nginx 两处配置（缺一不可）

`ops/dify-console-bridge/dify-console-10008.conf` 与 `ops/dify-login-proxy/dify-login-proxy.conf`
是**同一条链路的两半**：

- 10008 那一层用 `set_real_ip_from` + `real_ip_header` 修正 `$remote_addr`，
  并用 `map $realip_remote_addr $aiops_upstream_proto` 算出该上报的协议；
- snippet 里转发 `$aiops_upstream_proto`。

**只装 snippet 不装 10008** ⇒ `$aiops_upstream_proto` 未定义 ⇒ 登录代理拿不到
`X-Forwarded-Proto` ⇒ **所有登录被自己的闸门拒掉**。

### 4.1 传上去

（§1 已经 `cd` 到本仓并确认过 origin；若中途换了 shell，重跑那两行）

```bash
cd /home/claude/Projects/AI-Ops || exit 1
git show origin/main:ops/dify-console-bridge/dify-console-10008.conf > /tmp/new-10008.conf
git show origin/main:ops/dify-login-proxy/dify-login-proxy.conf   > /tmp/new-snippet.conf

scp /tmp/new-10008.conf yidong-36:/tmp/ngx-new-10008.conf
scp /tmp/new-snippet.conf yidong-36:/tmp/ngx-new-snippet.conf
```

### 4.2 先看 diff，再装

**必须先 diff** —— 线上可能有人改过，直接覆盖会抹掉别人的改动：

```bash
ssh yidong-36 'diff -u /www/server/panel/vhost/nginx/dify-console-10008.conf /tmp/ngx-new-10008.conf'
ssh yidong-36 'diff -u /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf /tmp/ngx-new-snippet.conf'
```

确认差异就是你要的那一处，然后装（备份在 §2 已做）：

```bash
ssh yidong-36 'cp /tmp/ngx-new-10008.conf /www/server/panel/vhost/nginx/dify-console-10008.conf
  cp /tmp/ngx-new-snippet.conf /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf'
```

### 4.3 在 **36 上**校验并 reload

```bash
ssh yidong-36 'nginx -t'            # 必须 ok 才继续；失败就按 §7 回滚
ssh yidong-36 'nginx -s reload'
```

⚠️ **这两条必须在 36 上跑**（注意 `ssh yidong-36`）。在开发机上执行只会动本机的 nginx。

### 4.4 怎么判断**运行中**的配置真的是新的

三种检查回答三个不同的问题，**不能互相替代**：

| 检查 | 回答的问题 | 不能回答 |
|---|---|---|
| `nginx -t` | 磁盘上的配置语法对不对 | 加载了没有 |
| `nginx -T \| grep -c …` | 磁盘上的文件里有没有这一行 | 加载了没有（它新起进程重新解析磁盘） |
| `systemctl is-active nginx` | 那个单元的状态 | 什么都不相关（该单元自 2026-09-11 起就 failed，进程由别的方式管理） |

**唯一能证明"运行中的 master 加载了它"的是行为**。而且判据必须**有区分性** ——
一个新旧配置都会给出的结果什么都证明不了。

用这一条（从**非回环**对端发，并伪造 `X-Forwarded-Proto: https`）：

```bash
ssh yidong-36 'curl -sS --interface 127.0.0.2 -o /dev/null -w "%{http_code}\n" \
  -X POST http://127.0.0.1:10008/__aiops/login \
  -H "Content-Type: application/json" -H "X-Forwarded-Proto: https" \
  -d "{\"username\":\"x\",\"password\":\"y\",\"randomStr\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"code\":\"zzzz\"}"'
# 期望 400（"此入口是明文 HTTP"）
```

**为什么这条有区分性**：新配置用 `map $realip_remote_addr` 只在真实对端是回环时采信上游
声明的协议；非回环对端送来的 `X-Forwarded-Proto` 被抹成空 ⇒ 代理判为明文 ⇒ 400。
**旧配置没有那段 map**，客户端自带的 `X-Forwarded-Proto: https` 会被原样透传 ⇒ 代理以为
是 TLS ⇒ 放行 ⇒ 403。所以 400 只可能是新配置，403 说明 reload 没生效。

⚠️ **反向的坑**：不带 `X-Forwarded-Proto` 的普通明文 POST 也返回 400，但那**不能**当判据 ——
代理自己的 TLS 闸门对任何缺这个头的请求都返 400，与 nginx 是新是旧无关。必须带上那个
伪造头、并**从非回环接口发**，才有区分性。

**若 36 上没有 `127.0.0.2`**（先 `ssh yidong-36 'curl --interface 127.0.0.2 -s -o /dev/null -w "%{http_code}" http://127.0.0.1:10008/__aiops/login'`
看它是否报 `bind failed`），**换一台机器从外部打 36 的公网 `:10008`**，不要再 `ssh`：

```bash
# 在**另一台**机器上执行（不是 36），目标写 36 的地址
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X POST http://36.156.159.175:10008/__aiops/login \
  -H "Content-Type: application/json" -H "X-Forwarded-Proto: https" \
  -d "{\"username\":\"x\",\"password\":\"y\",\"randomStr\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"code\":\"zzzz\"}"
# 期望 400
```

这条的区分性来自**对端不是回环**（是那台外部机器），同样成立。注意三点：
不要 `ssh yidong-36`、不要 `--interface 127.0.0.2`、目标必须是 36 的地址而非 `127.0.0.1`。

## 5. 重启与版本核对

```bash
ssh yidong-36 'systemctl restart aiops-dify-login-proxy && sleep 3
  systemctl is-active aiops-dify-login-proxy'
ssh yidong-36 'sha256sum /opt/aiops-login-proxy/aiops_diagnostics/dify_login_server.py'
git show origin/main:src/aiops_diagnostics/dify_login_server.py | sha256sum   # 两个必须相等
```

## 6. 验收（四条，都是真入口）

| # | 动作 | 期望 |
|---|---|---|
| A | `POST http://36.156.159.175:10008/__aiops/login` 带明文口令 | 400「此入口是明文 HTTP…请使用 https://dify.ranlei.work 登录」 |
| B | `GET` 同入口的验证码 | 200 `image/jpeg` |
| C | `https://dify.ranlei.work` 取图 + **故意填错验证码** | 403「验证码不正确或已失效」 |
| D | 同一来源连续失败到阈值 | 第 N 次起 429 |

**C 必须用错码**。正确验证码 + 错误密码得到的是「密码不正确」——那条路修复前后都是 403，
**证明不了任何分类修复**。错码才是走 428 分支的那条。

D 的日志应出现 `source=<真实地址>`，**不是 `127.0.0.1`**（那是 realip 修正生效的判据）。

## 7. 回滚

**三处备份是两个时间戳** —— §2 里代码与 nginx 是在两条命令里分别备份的，时间戳不同
（2026-10-19 那次：代码 `20261019182443`、nginx `20261019182623`）。先看清楚：

```bash
ssh yidong-36 'ls -d /opt/aiops-login-proxy.bak-*
  ls /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-*
  ls /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-*'
```

然后填**两个**变量：

```bash
CODE_TS=20261019182443    # ← 代码包那个
NGX_TS=20261019182623     # ← nginx 那个
ssh yidong-36 "set -e
  test -d /opt/aiops-login-proxy.bak-$CODE_TS            # 存在性先验，缺了就停
  test -f /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-$NGX_TS
  test -f /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-$NGX_TS
  systemctl stop aiops-dify-login-proxy
  cp -a /opt/aiops-login-proxy.bak-$CODE_TS/. /opt/aiops-login-proxy/
  cp /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-$NGX_TS \
     /www/server/panel/vhost/nginx/dify-console-10008.conf
  cp /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-$NGX_TS \
     /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf
  nginx -t
  nginx -s reload
  systemctl start aiops-dify-login-proxy"
```

四处要点：

1. **两份 nginx 配置都要恢复**，不能只恢复一份 —— snippet 引用 10008 那一层定义的
   `$aiops_upstream_proto`，只回一半会比回滚前更坏（所有登录被拒）。
2. `set -e`：任何一步失败就停，不会出现"配置没恢复但服务已启动"。
3. **`nginx -t` 在 `nginx -s reload` 之前**；`-t` 失败就不要 reload（旧配置还在跑，
   服务停止中，此时人工介入比 reload 坏配置安全）。
4. 代理**在停服务与恢复文件之间**处于停止状态。若中途失败，先别急着 `start` ——
   先把文件恢复完，再起服务；起之前用 §6 的 A/B/C 三条确认。

nginx 两处要**一起**回滚（snippet 引用了 10008 那一层定义的变量）。

## 8. 已知的坑

| 现象 | 真因 |
|---|---|
| `No module named aiops_diagnostics.dify_login_server` | 属主/权限不对（见 §3），不是文件缺失 |
| `systemctl is-active nginx` = failed | 既有失效单元，端口才是判据 |
| 所有登录都 400「明文 HTTP」 | 10008 那一层的 realip/map 没装或没 reload |
| 登录后立刻弹回 `/signin` | 走了明文入口；会话 cookie 是 `Secure` + `__Host-`，http 源上被丢弃 |
