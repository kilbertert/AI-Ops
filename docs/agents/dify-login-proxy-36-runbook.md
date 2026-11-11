# 36 上部署登录代理（可复跑的手顺）

> **读者**：要在 36 上更新 `aiops-dify-login-proxy` 或它那两个 nginx 配置的人。
> **状态**：2026-11-11补齐完整代码升级、服务身份预检和SQLite副本回放；旧nginx专项步骤保留。
> 决定的论证在 ADR-0023 / 0052 / 0053 / 0055；本页只讲**怎么做、怎么判、怎么回滚**。

## 0. 三句话

1. 登录代理复用人工升级手顺，接收已合并修订的完整代码制品；Gateway标准CD成功不代表它已同步。
2. **解包后必须核对属主** —— `tar` 会把归档里的属主/权限一起带过去，而服务身份是
   `aiopslogin`。这一步漏掉的表现是 `No module named aiops_diagnostics.dify.dify_login_server`
   （**报错说模块不存在，实际是读不到文件**）。
3. **判断入口是否生效看端口与 `nginx -T`，不看 `systemctl is-active nginx`** ——
   那个单元从 2026-09-11 起就是 failed（既有债，报 "already running"）。

## 0.1 模块归位后的代码/服务单元配套

新合并源码使用`aiops_diagnostics.dify.dify_login_server`。先按§1–3准备完整候选并通过预检，
备份代码、环境文件与单元，再按§5停止并完整切换。仅当旧单元仍引用旧模块时才需要下列改写：

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

## 1. 从合并修订取完整制品

在规范checkout核对origin与已合并提交，固定`LOGIN_RELEASE_SHA`，不要从未提交工作树打包。
位置由[项目主机声明](dev-host-declaration.md)及该服务现役单元/环境记录确定：
`LOGIN_TARGET`是声明的逻辑目标，`LOGIN_ARTIFACT_DEST`是制品接收位置，
`LOGIN_CANDIDATE_ROOT`是候选代码目录，`LOGIN_DATA_ROOT`是服务数据目录；
地址、身份访问材料与配置值留私有记录。以下变量须先按本次获准目标填好，不能复制其他服务的值。

```bash
set -eu
umask 027
: "${LOGIN_RELEASE_SHA:?填写本次批准的合并提交}"
: "${LOGIN_TARGET:?填写项目声明的逻辑目标}"
: "${LOGIN_ARTIFACT_DEST:?填写声明允许的制品接收位置}"
git fetch --prune origin
LOGIN_RELEASE_SHA=$(git rev-parse --verify "${LOGIN_RELEASE_SHA}^{commit}")
git merge-base --is-ancestor "$LOGIN_RELEASE_SHA" origin/main
mkdir -p dist/login-proxy
LOGIN_ARTIFACT="dist/login-proxy/${LOGIN_RELEASE_SHA}.tar"
git archive --format=tar --output="$LOGIN_ARTIFACT" "$LOGIN_RELEASE_SHA" src
LOGIN_ARTIFACT_SHA=$(sha256sum "$LOGIN_ARTIFACT" | cut -d ' ' -f1)
dev-host cp "$LOGIN_TARGET" "$LOGIN_ARTIFACT" --dest "$LOGIN_ARTIFACT_DEST" \
  --artifact-sha256 "$LOGIN_ARTIFACT_SHA"
```

本制品包含`src/aiops_diagnostics/`。在确认候选目录是本次新建的空目录后，
以`tar -xf "$LOGIN_ARTIFACT_DEST" -C "$LOGIN_CANDIDATE_ROOT" --strip-components=1`解包：
候选根直接包含`aiops_diagnostics/`，导入与切换沿用现役包根布局，不额外嵌套一层`src`。
旧精简包可能缺少后来拆出的`common`等模块，必须交付完整包，不能只替换一个store或工具文件。
传输后复核制品摘要和关键模块SHA256；摘要只标识字节，合并来源仍须单独核对。

## 2. 路径权限与备份前置

候选在现役代码旁的独立目录展开，旧服务保持运行。使用实际服务User/Group检查每级父目录的
遍历权限与候选文件可读性，`root`能导入不代表服务身份能导入。用`namei -l`定位阻断的父目录，
并以服务身份执行`test -x`/`test -r`及§3的导入；若单元有沙箱/挂载限制，预检须复用相同限制。

**候选不能放在服务身份不可遍历的`/tmp`下。** 若受限，选择已批准、可遍历的代码候选位置；
只修候选目录自身的属主/模式，不修改主机`/tmp`权限，不为通过预检扩大共享目录访问。
临时SQLite副本放在服务数据目录内由服务身份创建的私有临时子目录，不覆盖正式文件。

切换前分别备份并记录代码目录、环境文件、服务单元的原路径、备份位置、摘要与恢复顺序。
配置备份使用`cp -a`，并用`stat -c '%u:%g %a'`记录原文件UID、GID与mode、核对备份保留的元数据。
备份目录使用本次唯一标识，先确认目标不存在，避免`mv`遇到已有目录时意外嵌套；恢复前同样检查失败代码留存位置。
环境文件和单元即使本次不改也保留备份；不打印配置正文。仅当本次另改nginx时才备份并执行§4/§7的配置步骤。

## 3. 真实服务身份预检与SQLite一致副本

通过`dev-host exec "$LOGIN_TARGET" --allow-service-exec -- <受控命令>`执行既有手顺，
远端命令须按`dev-host`的shell引用规则传递；Python短探针走stdin，不落到共享临时脚本路径。
以下步骤任一失败即停止，旧服务继续运行，不提前切换：

1. 使用现役Python与真实服务身份，从候选包根导入`LoginProxySettings`和`build_service`，
   确认模块`__file__`指向候选。现役环境由获准的本地读取器仅在内存获取，沿用§3.5按数据解析的原则；
   不source环境文件、不打印settings或`/proc/.../environ`，不把凭据放进命令参数或证据。
2. 用`LoginProxySettings.from_env()`校验现役配置。执行`build_service`**之前**，必须用
   `dataclasses.replace(settings, data_dir=临时副本目录)`替换数据根；否则构造会打开正式mappings.db。
3. 对正式mappings.db只读连接使用`sqlite3.Connection.backup()`生成临时一致副本，
   不热拷主文件而遗漏WAL。连接都显式关闭；原库与副本路径分开断言，副本权限保持私有。
4. 保存副本原有业务表schema、行数及原列逻辑摘要；调用候选`build_service`后重算对照，
   执行`PRAGMA quick_check`。比较逻辑数据而非数据库文件hash，不能因页布局改变误判丢数据。
   若有列追加，只对原列核对数据保留并单列新schema结果，不把未知差异自动放行。
5. 关闭GC后重复调用`MappingStore.get`，对比fd数量；同时确认正常写上下文提交、异常回滚与关闭
   的既有验收仍适用。只输出计数、判定和代码身份，不输出映射行或账号内容。
6. 清理本轮临时副本并恢复GC；不调用公司登录、开户或发布接口。该预检证明候选导入、配置和
   副本数据兼容，不能代替§6的正常登录验收。

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

## 5. 完整代码切换与版本核对

仅在§3通过且§2三类备份完成后，按现役单元名执行以下顺序：

1. `systemctl stop`登录代理，确认进程已停；保留正式数据目录及数据库。
2. 将现役完整代码目录rename为本次备份，将完整候选rename到现役位置；不要覆盖解包或混留旧模块。
   提前确认两目录可在同一文件系统rename，且切换后的布局、属主与权限符合原单元。
3. 仅在本次确实改过unit时`systemctl daemon-reload`；`systemctl start`后检查active、进程身份和
   关键模块SHA256与合并制品一致。环境文件和单元未修改时保持原值。
4. 通过§6正常重登并核对apps、原logs页面、manage与run-list；记录首次失败和重试条件。
   失败按§7恢复完整代码，不能用恢复旧数据库掩盖新代码问题。

### 5.1 重启后的公司令牌缓存

代理内存中的公司token缓存随重启失效；浏览器Dify cookie尚有效也可能使manage返回403、页面提示
“公司登录已过期”。这次403必须记录，不能把已有cookie视作公司会话仍有效。
用正常公司账号登录流程和本轮验证码重新登录，使用新返回会话复验；不重放旧token、不伪造缓存、
不修改数据库使会话通过。凭据只从受控资产在内存取得，截图和原始响应保持私有。

若公网直连TLS reset/timeout，按[既有多租户访问方法](operator-cpms/multitenant.md)使用获准的
受控动态回环隧道；记录取证通道，结束后清理进程组并确认端口消失。通道可用不等于公网故障已修复。

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

### 7.1 仅代码升级失败

停止登录代理，将失败候选从现役位置移开，再将本次完整代码备份恢复到原位置；核对目录与文件权限后启动。
本次没有改env/unit/nginx时不恢复它们。若本次确实改过unit，只恢复本次对应备份并daemon-reload。
按§5核代码身份，再正常重登复验；失败代码保留供定位，不能把旧文件overlay到新目录留下混版。

**只回滚代码，不回滚数据库。** 正式DB从切换前到恢复后都保持原位，不从预检临时副本或旧备份覆盖；
否则会丢失切换后合法写入。数据库恢复是另行授权的事故操作，本手顺不包含它。

### 7.2 一个回滚入口，配置按本次变更独立恢复

本次阶段1只切换代码，三个`*_CHANGED`均为0。若某次还改过env、unit或nginx，分别设为1：
只启用的配置才要求备份并恢复；unit恢复后才daemon-reload，nginx恢复后才校验并reload。
两份nginx配置相互依赖，修改该链路时一起备份/恢复。环境标识影响账号派生，改过env也必须恢复，
但不因此要求nginx备份。数据库不在任何恢复路径中。

以下是**在已确认的目标主机维护shell里执行**的一个Bash示例，不要在开发机直接执行。
通过既有`dev-host`受控操作入口进入目标；从本次备份记录逐项填写路径，先复核代码布局与权限。
所有路径都须是无符号链接、无`..`且不以`/`结尾的绝对路径；代码和配置位置不能相同或互相嵌套。
`LOGIN_READY=NO`使原样粘贴停止；确认目标、备份与变更标志后才改成`YES`，占位路径仍会被拒绝。
`env-only`指代码回退同时仅恢复env；它不要求unit或nginx有备份。
恢复前按备份记录核对现役目标与备份的UID/GID/mode；现役目标不符合原记录时先停止并核实原因，
不要靠复制备份元数据覆盖问题。下例只向已存在的普通配置文件恢复内容，保留目标属主、属组和mode。
系统服务的`EnvironmentFile=`由systemd系统管理器（root）读取，并非由代理进程读取；
备份属组错误是权限漂移风险，不能据此断言代理必然启动失败。服务身份直接读文件的预检另核读权限。

```bash
set -euo pipefail
LOGIN_READY=NO
LOGIN_SERVICE='REPLACE.service'
LOGIN_ACTIVE_ROOT='/REPLACE/active-code'
LOGIN_CODE_BACKUP='/REPLACE/code-backup'
LOGIN_FAILED_ROOT='/REPLACE/failed-code'
LOGIN_ENV_CHANGED=0
LOGIN_ENV_FILE='/REPLACE/current.env'
LOGIN_ENV_BACKUP='/REPLACE/backup.env'
LOGIN_UNIT_CHANGED=0
LOGIN_UNIT_FILE='/REPLACE/current.service'
LOGIN_UNIT_BACKUP='/REPLACE/backup.service'
LOGIN_NGINX_CHANGED=0
LOGIN_NGINX_SITE='/REPLACE/current-site.conf'
LOGIN_NGINX_SITE_BACKUP='/REPLACE/backup-site.conf'
LOGIN_NGINX_SNIPPET='/REPLACE/current-snippet.conf'
LOGIN_NGINX_SNIPPET_BACKUP='/REPLACE/backup-snippet.conf'

[ "$LOGIN_READY" = YES ] || { echo '未确认目标与备份，停止'; exit 1; }
[[ "$LOGIN_SERVICE" =~ ^[A-Za-z0-9_.@-]+\.service$ && "$LOGIN_SERVICE" != *REPLACE* ]]
for flag in "$LOGIN_ENV_CHANGED" "$LOGIN_UNIT_CHANGED" "$LOGIN_NGINX_CHANGED"; do
  [[ "$flag" = 0 || "$flag" = 1 ]]
done
login_files=(); login_backups=()
if [ "$LOGIN_ENV_CHANGED" = 1 ]; then
  login_files+=("$LOGIN_ENV_FILE"); login_backups+=("$LOGIN_ENV_BACKUP")
fi
if [ "$LOGIN_UNIT_CHANGED" = 1 ]; then
  [[ "${LOGIN_UNIT_FILE##*/}" = "$LOGIN_SERVICE" ]]
  login_files+=("$LOGIN_UNIT_FILE"); login_backups+=("$LOGIN_UNIT_BACKUP")
fi
if [ "$LOGIN_NGINX_CHANGED" = 1 ]; then
  login_files+=("$LOGIN_NGINX_SITE" "$LOGIN_NGINX_SNIPPET")
  login_backups+=("$LOGIN_NGINX_SITE_BACKUP" "$LOGIN_NGINX_SNIPPET_BACKUP")
fi
login_paths=("$LOGIN_ACTIVE_ROOT" "$LOGIN_CODE_BACKUP" "$LOGIN_FAILED_ROOT"
             "${login_files[@]}" "${login_backups[@]}")
for path in "${login_paths[@]}"; do
  [[ "$path" = /* && "$path" != / && "$path" != *REPLACE* ]]
  [[ "$(realpath -m -- "$path")" = "$path" ]]
done
for ((i=0; i<${#login_paths[@]}; i++)); do
  for ((j=i+1; j<${#login_paths[@]}; j++)); do
    a="${login_paths[i]}/"; b="${login_paths[j]}/"
    [[ "$a" != "$b"* && "$b" != "$a"* ]]
    if [ -e "${login_paths[i]}" ] && [ -e "${login_paths[j]}" ]; then
      [[ ! "${login_paths[i]}" -ef "${login_paths[j]}" ]]
    fi
  done
done
[[ -d "$LOGIN_ACTIVE_ROOT" && -d "$LOGIN_CODE_BACKUP" ]]
[[ ! -e "$LOGIN_FAILED_ROOT" && ! -L "$LOGIN_FAILED_ROOT" ]]
[[ -d "$(dirname -- "$LOGIN_FAILED_ROOT")" ]]
[[ "$(stat -c %d -- "$LOGIN_ACTIVE_ROOT")" = "$(stat -c %d -- "$LOGIN_CODE_BACKUP")" ]]
[[ "$(stat -c %d -- "$LOGIN_ACTIVE_ROOT")" = "$(stat -c %d -- "$(dirname -- "$LOGIN_FAILED_ROOT")")" ]]
for path in "${login_files[@]}" "${login_backups[@]}"; do
  [[ -f "$path" ]]
done
command -v systemctl >/dev/null
if [ "$LOGIN_NGINX_CHANGED" = 1 ]; then command -v nginx >/dev/null; fi

# 所有前置检查完成后才停止服务；失败中止，不自动启动半恢复的服务。
systemctl stop "$LOGIN_SERVICE"
mv -- "$LOGIN_ACTIVE_ROOT" "$LOGIN_FAILED_ROOT"
mv -- "$LOGIN_CODE_BACKUP" "$LOGIN_ACTIVE_ROOT"
for i in "${!login_files[@]}"; do
  cp --no-preserve=mode,ownership -- "${login_backups[i]}" "${login_files[i]}"
done
if [ "$LOGIN_UNIT_CHANGED" = 1 ]; then systemctl daemon-reload; fi
if [ "$LOGIN_NGINX_CHANGED" = 1 ]; then
  nginx -t
  nginx -s reload
fi
systemctl start "$LOGIN_SERVICE"
systemctl is-active --quiet "$LOGIN_SERVICE"
```

`nginx -t`失败就不reload、不start登录代理；保留失败位置，修正本次恢复文件后再按批准步骤继续。
退出成功仍须按§5核对实际加载的代码身份，再按§5.1正常重登；不要用恢复旧DB绕开登录或数据问题。

## 8. 已知的坑

| 现象 | 真因 |
|---|---|
| `No module named aiops_diagnostics.dify.dify_login_server` | 属主/权限不对（见 §3），不是文件缺失 |
| `systemctl is-active nginx` = failed | 既有失效单元，端口才是判据 |
| 所有登录都 400「明文 HTTP」 | 10008 那一层的 realip/map 没装或没 reload |
| 登录后立刻弹回 `/signin` | 走了明文入口；会话 cookie 是 `Secure` + `__Host-`，http 源上被丢弃 |
