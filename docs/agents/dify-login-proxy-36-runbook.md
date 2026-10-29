# 36 上部署登录代理（可复跑的手顺）

> **读者**：要在 36 上更新 `aiops-dify-login-proxy` 或它那两个 nginx 配置的人。
> **状态**：2026-10-19 首次成文（#849 上线时实操并踩坑）。
> 决定的论证在 ADR-0023 / 0052 / 0053 / 0055；本页只讲**怎么做、怎么判、怎么回滚**。

## 0. 三句话

1. **代码是手工拷贝**，不是 CD。`/opt/aiops-login-proxy/` 不在仓库部署管线里。
2. **解包后必须核对属主** —— `tar -xzf` 会把归档里的属主/权限一起带过去，而服务身份是
   `aiopslogin`。这一步漏掉的表现是 `No module named aiops_diagnostics.dify.dify_login_server`
   （**报错说模块不存在，实际是读不到文件**）。
3. **判断入口是否生效看端口与 `nginx -T`，不看 `systemctl is-active nginx`** ——
   那个单元从 2026-09-11 起就是 failed（既有债，报 "already running"）。

## 0.1 模块归位后的代码/服务单元配套

新合并源码使用`aiops_diagnostics.dify.dify_login_server`。更新前备份整个代码目录与现役服务单元，
停止登录代理，以完整制品替换`aiops_diagnostics/`（不要覆盖解压而残留旧模块），再仅修改ExecStart里的模块路径：

```python
from pathlib import Path

unit = Path("/etc/systemd/system/aiops-dify-login-proxy.service")
text = unit.read_text()
old = "-m aiops_diagnostics.dify_login_server"
new = "-m aiops_diagnostics.dify.dify_login_server"
assert text.count(old) + text.count(new) == 1
unit.write_text(text.replace(old, new))
```

执行`systemctl daemon-reload`后按后文启动和验证。失败恢复代码及原单元、daemon-reload再启动，
不倒回映射数据库。原服务User/Group和环境项保持不变。重启会清空近期公司令牌缓存，须正常重登。
以下取制品命令里的完整包已经包含新dify子目录；不使用旧模块名探测新包。

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

**环境文件也要备份，并且确认 `AIOPS_LOGIN_PROXY_ENVIRONMENT` 在不在**（#864 / ADR-0061）：

```bash
ssh yidong-36 'sudo cp -a /etc/aiops-login-proxy/login-proxy.env \
                 /etc/aiops-login-proxy/login-proxy.env.bak-$(date +%Y%m%d%H%M%S)
  sudo grep -n ^AIOPS_LOGIN_PROXY_ENVIRONMENT= /etc/aiops-login-proxy/login-proxy.env'
```

没有这一行，新代码**起不来**（`ValueError: AIOPS_LOGIN_PROXY_ENVIRONMENT is required`）——
这是刻意的，见 §3.5。补值时取**这台代理服务的公司环境**（41 → `41`，95 → `95`），
**不能照抄另一台**：两台填同一个值，两套 UPMS 的同名 `sys_user.id` 又会落回同一个 Dify 账号。

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

## 3.5 环境标识（#864）

代码会读 `AIOPS_LOGIN_PROXY_ENVIRONMENT`，它参与 Dify 账号邮箱的派生
（`{environment}-{公司用户id}@aiops-login.invalid`）。**部署完先确认它在**：

```bash
ssh yidong-36 'sudo grep -n ^AIOPS_LOGIN_PROXY_ENVIRONMENT= /etc/aiops-login-proxy/login-proxy.env'
```

判据不是"文件里有这一行"，而是**两道闸门各自都会拦**，而且用的是**部署出来的那份代码**：

**闸门一：`LoginProxySettings.from_env` 的必填检查**（漏配 ⇒ 服务起不来）。
用**真实的环境文件**，但把它当**数据**读、按 `KEY=VALUE` 拆开 —— 不要用
`. 环境文件` 去 source 它：那会让 shell 以执行者身份运行文件里的任意命令，而服务本身
（systemd `EnvironmentFile=`）只做变量解析、从不执行。以 `aiopslogin` 跑（它对该文件有
组读权限），**不需要 root**：

```bash
ssh yidong-36 "sudo -n -u aiopslogin /usr/bin/python3 - <<'PY'
import os, sys
sys.path.insert(0, '/opt/aiops-login-proxy')
from aiops_diagnostics.dify.dify_login_server import LoginProxySettings

env = {}
for line in open('/etc/aiops-login-proxy/login-proxy.env', encoding='utf-8'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    env[k.strip()] = v.strip()

os.environ.clear(); os.environ.update(env)
try:
    print('present -> STARTED environment=', LoginProxySettings.from_env().dify.environment)
except Exception as e:
    print('present ->', type(e).__name__, e)
os.environ.pop('AIOPS_LOGIN_PROXY_ENVIRONMENT', None)
try:
    LoginProxySettings.from_env(); print('missing -> STARTED (bad)')
except Exception as e:
    print('missing ->', type(e).__name__, e)
PY"
# 期望：present -> STARTED environment= 41   （实际配置值真的被读到）
#       missing -> ValueError AIOPS_LOGIN_PROXY_ENVIRONMENT is required
```

⚠️ **不要 `sudo bash -c 'set -a; . 环境文件; …'`**（评审指出）：那是把生产配置文件
当 shell 脚本、以 **root** 身份执行。文件今天是干净的，但这条手顺会教下一个人把
"以 root 执行一个可被编辑的文件"当成常规操作 —— 一旦有人往里放一行命令，就是 root 执行。
服务自己从不这么做，判据也不该这么做。

**闸门二：`DifyConsoleSettings` 的格式校验**（配了但写法不合法 ⇒ 同样起不来）。
这一道**必须直接构造 settings** —— 它要证明的是派生那条判据与构造那条是同一条：

```bash
ssh yidong-36 'sudo -n -u aiopslogin /usr/bin/python3 - <<PY
import sys
sys.path.insert(0, "/opt/aiops-login-proxy")
from aiops_diagnostics.dify.dify_login_provision import DifyConsoleSettings
for v in ("", "prod.41"):
    try:
        DifyConsoleSettings(base_url="http://x", secret_key="x", environment=v)
        print(repr(v), "-> STARTED (bad)")
    except Exception as e:
        print(repr(v), "->", type(e).__name__)
PY'
# 期望：两行都是 DifyProvisionError
```

⚠️ **两道闸门不能互相代替**（评审指出）：闸门二直接传空串，测的是 `DifyConsoleSettings`
自己那条判据；如果服务入口哪天不再调用 `required(...)`，闸门二照样通过、而漏配会
以"环境为空"的形式走到派生处。反过来，闸门一不校验格式。所以两条都要跑。

⚠️ **脚本从 stdin 走，不要先 `cat > /tmp/xxx.py` 再 sudo 执行**：`/tmp` 是共享目录，
别人可以先把那个路径占住、写成自己的内容，而 `sudo -u aiopslogin` 会以**代理身份**
执行它 —— 那个身份读得到 `login-proxy.env`（含 `SECRET_KEY`）。stdin 没有这个窗口，
也不会留下一个可被替换的文件。

⚠️ 两个探针都**只构造 settings**（闸门一会读环境文件，那是它的判据本身）：
不连公司 UPMS、不连 Dify、不碰生产映射表，也不写任何文件。

## 3.6 四键（#883）与 `GATEWAY_URL` 填什么（2026-10-22 踩过）

按 #883，代理要在登录时把这个人 join 进他租户的 workspace。四键**成对**，全空 = 维持单一固定工作区。

| 键 | 填什么 | 不要填 |
|---|---|---|
| `GATEWAY_URL` | 那个环境的**公网入口**，如 `https://api.mall.qushiyun.com` | ❌ 网关主机的本机地址（`172.18.0.1:8788`） |
| `GATEWAY_INTERNAL_TOKEN` | 41 `gateway.env` 里 `AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET` 的**同一个值** | — |
| `INNER_API_BASE_URL` | 36 本机转发器 `http://127.0.0.1:15001` | — |
| `INNER_API_KEY` | 与 41 `production.env` 的 `AIOPS_GATEWAY_DIFY_INNER_API_KEY` **同一把** | — |

**为什么 `GATEWAY_URL` 必须是公网**：代理在 36，网关在 41。`172.18.0.1` 在**两台机器上都是各自的 docker 网桥** —— 在 36 上填它，请求打到 36 自己的网桥然后超时，登录被拒，错误码 `DIFY_WORKSPACE_LOOKUP_FAILED`（措辞把人引向"workspace 没建"，而实际是网络）。

**为什么不用 SSH 隧道**：同一台 36 上已有的发布桥（`AIOPS_CONSOLE_BRIDGE_GATEWAY_URL`）走的就是同一个公网入口。隧道只在公网**根本不可达**时才成立 —— 41→36 的 inner_api 是那种情形（Dify 的 5001 不对外发布）。给这个跳转拉隧道，会让"加一个环境"变成一次主机运维动作，而多租户本就按环境线性增长。

**41 侧的前置**（这一轮才发现一直缺）：`GET /v1/dify/workspace` 在 `AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET` 未配置时**整条路由 404**。两个 PR 各自都绿，合起来是死链；而 join 失败按设计**不阻断登录** ⇒ 没有任何报警。

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
ssh yidong-36 'sha256sum /opt/aiops-login-proxy/aiops_diagnostics/dify/dify_login_server.py'
git show origin/main:src/aiops_diagnostics/dify/dify_login_server.py | sha256sum   # 两个必须相等
```

## 6. 验收（五条，都是真入口）

| # | 动作 | 期望 |
|---|---|---|
| A | `POST http://36.156.159.175:10008/__aiops/login` 带明文口令 | 400「此入口是明文 HTTP…请使用 https://dify.ranlei.work 登录」 |
| B | `GET` 同入口的验证码 | 200 `image/jpeg` |
| C | `https://dify.ranlei.work` 取图 + **故意填错验证码** | 403「验证码不正确或已失效」 |
| D | 同一来源连续失败到阈值 | 第 N 次起 429 |

**C 必须用错码**。正确验证码 + 错误密码得到的是「密码不正确」——那条路修复前后都是 403，
**证明不了任何分类修复**。错码才是走 428 分支的那条。

D 的日志应出现 `source=<真实地址>`，**不是 `127.0.0.1`**（那是 realip 修正生效的判据）。

E. `POST http://36.156.159.175:10008/__aiops/login` 带伪造头 `X-Forwarded-Proto: https`
   且**从非回环对端发**（换一台机器或 `curl --interface 127.0.0.2`）⇒ 期望 **400**
   「此入口是明文 HTTP…」。

**D 与 E 不是一回事，不能互相替代**：D 只证明 `realip` 把 `$remote_addr` 改写对了；
`$aiops_upstream_proto` 是另一个 `map` 算的、只喂 `X-Forwarded-Proto`。一个 realip 正常
但协议 `map` 没加载的部署，D 照样通过。E 才有区分性：新配置只在真实对端是回环时采信
上游声明的协议，所以伪造的 `https` 被抹空 ⇒ 判明文 ⇒ 400；旧配置原样透传 ⇒ 以为是 TLS
⇒ 403。**400 只可能是新配置。**

## 7. 回滚

**四处备份、三个时间戳** —— 代码、两个 nginx 文件、环境文件是在不同命令里分别备份的。
两个 nginx 文件通常共用一条命令、一个时间戳，另外两处各自不同（2026-10-19 那次：
代码 `20261019182443`、nginx `20261019182623`）。先看清楚：

```bash
ssh yidong-36 'ls -d /opt/aiops-login-proxy.bak-*
  ls /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-*
  ls /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-*
  sudo ls /etc/aiops-login-proxy/login-proxy.env.bak-*'
```

然后填**三个**变量（环境文件那份只有在你改过它时才有；没改过就不要恢复它，
否则会把别人后加的键退回去）：

```bash
CODE_TS=20261019182443    # ← 代码包那个
NGX_TS=20261019182623     # ← nginx 那个
ENV_TS=20261020180542     # ← 环境文件那个；没改过环境文件就留空
ssh yidong-36 "set -e
  test -d /opt/aiops-login-proxy.bak-$CODE_TS            # 存在性先验，缺了就停
  test -f /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-$NGX_TS
  test -f /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-$NGX_TS
  if [ -n \"$ENV_TS\" ]; then test -f /etc/aiops-login-proxy/login-proxy.env.bak-$ENV_TS; fi
  systemctl stop aiops-dify-login-proxy
  cp -a /opt/aiops-login-proxy.bak-$CODE_TS/. /opt/aiops-login-proxy/
  cp /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-$NGX_TS \
     /www/server/panel/vhost/nginx/dify-console-10008.conf
  cp /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-$NGX_TS \
     /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf
  if [ -n \"$ENV_TS\" ]; then cp /etc/aiops-login-proxy/login-proxy.env.bak-$ENV_TS \
     /etc/aiops-login-proxy/login-proxy.env; fi
  nginx -t
  nginx -s reload
  systemctl start aiops-dify-login-proxy"
```

**为什么环境文件也在回滚范围里**（#864）：`AIOPS_LOGIN_PROXY_ENVIRONMENT` 决定账号邮箱
的前缀。回滚代码却留下一个改错的环境值，下一次部署会**在错误的环境前缀下派生账号** ——
那是静默的、只有在两个环境撞号时才显形的错。`ENV_TS` 留空是给"这次没动环境文件"
的情形，不是省略它的理由。

四处要点：

1. **两份 nginx 配置都要恢复**，不能只恢复一份 —— snippet 引用 10008 那一层定义的
   `$aiops_upstream_proto`，只回一半会比回滚前更坏（所有登录被拒）。
2. `set -e`：任何一步失败就停，不会出现"配置没恢复但服务已启动"。
3. **`nginx -t` 在 `nginx -s reload` 之前**；`-t` 失败就不要 reload（旧配置还在跑，
   服务停止中，此时人工介入比 reload 坏配置安全）。
4. 代理**在停服务与恢复文件之间**处于停止状态。若中途失败，先别急着 `start` ——
   先把文件恢复完，再起服务；起之前用 §6 的 A/B/C 三条确认，并重跑 §3.5 的探针
   （回滚后的代码可能还不认识 `AIOPS_LOGIN_PROXY_ENVIRONMENT`，那也正是要回滚的情形之一）。

nginx 两处要**一起**回滚（snippet 引用了 10008 那一层定义的变量）。

## 8. 已知的坑

| 现象 | 真因 |
|---|---|
| `No module named aiops_diagnostics.dify.dify_login_server` | 属主/权限不对（见 §3），不是文件缺失 |
| `systemctl is-active nginx` = failed | 既有失效单元，端口才是判据 |
| 所有登录都 400「明文 HTTP」 | 10008 那一层的 realip/map 没装或没 reload |
| 登录后立刻弹回 `/signin` | 走了明文入口；会话 cookie 是 `Secure` + `__Host-`，http 源上被丢弃 |
