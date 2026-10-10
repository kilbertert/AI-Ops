# 41 环境运维与真实验收手册

> **用途**：让任何 agent/工程师在不依赖会话记忆的前提下，能独立完成 41
> （`api.mall.qushiyun.com`）的**部署、真实资源创建、真实端到端验收**。
> **最后核验**：2026-09-16（对 main `b0b3856` 实测；新增 §1.1 密钥登录）。
>
> **凭据纪律**：本文件只写方法与位置，**不写任何口令/令牌/thirdSession 明文**。
> 凭据读取一律从 41 主机的 root 权限环境变量现场取，不落库、不入仓库、不贴聊天。

## 0. 环境事实（一次核对）

| 事实 | 值 |
|---|---|
| 41 主机 | `47.97.160.153`（root 登录，方式见 §1） |
| 运行源码 | `/opt/aiops-41/src/aiops_diagnostics/` |
| Gateway 数据库 | `/var/lib/aiops-41/gateway/gateway.db`（SQLite） |
| 服务配置（**服务端配置**） | `/etc/aiops-41/production.env`（含凭据，勿打印明文）。由 `AIOPS_GATEWAY_SERVER_CONFIG_FILE` 指向，供 `Settings.from_config` 读取：模型 provider、KB、Redis 等**配置项**在这里 |
| 服务配置（**进程环境**） | `/etc/aiops-41/gateway.env`。systemd 单元的 `EnvironmentFile` 是**它**，不是 `production.env` |
| 平台清单 | `/opt/aiops-41/ops/environments/env-41.toml` |
| 服务单元 | `aiops-gateway-41.service`、`aiops-36-kb-tunnel.service` |
| Gateway 监听 | **`172.18.0.1:8788`**（宿主侧 docker 网桥；2026-09-30 D-2 起，原为 `127.0.0.1`）—— **本机回环不再监听**，`curl 127.0.0.1:8788` 会「连不上」，那不代表服务挂了 |
| KB 隧道 | `127.0.0.1:29380`（→ 36 kb-service） |
| 公网入口 | `https://api.mall.qushiyun.com/v1/*`（**不是** `/aiops/v1/*`，后者 404） |
| 会话 Redis | `127.0.0.1:6379`（41 本机），键前缀 `app:3rd_session:` |
| 业务库 | MySQL `192.168.1.45:3306/cloud_charging_pile`（账号 `mall`，只读用途） |

## 1. 访问 41

### 1.1 首选：密钥登录（2026-09-16 起已配置）

```bash
ssh aiops-41 '<命令>'          # 别名：47.97.160.153, user root, key id_ed25519_41_aiops
```

别名定义在 `~/.ssh/config`；私钥 `~/.ssh/id_ed25519_41_aiops`（公钥已装到 41 的
`/root/.ssh/authorized_keys`）。**不需要口令，可直接用于自动化。** 验证：

```bash
ssh -o BatchMode=yes aiops-41 'echo OK; hostname'
```

若 `Permission denied (publickey)`：说明公钥未装或私钥缺失，按 §1.2 用一次性口令装回公钥。

### 1.2 一次性口令（仅用于装回公钥；口令现场从受控来源取，不写进任何文件）

```bash
PUB=$(cat ~/.ssh/id_ed25519_41_aiops.pub)
SSHPASS='<现场从受控来源取得>' sshpass -e ssh -o StrictHostKeyChecking=no root@47.97.160.153 "
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
  grep -qF '$PUB' /root/.ssh/authorized_keys || echo '$PUB' >> /root/.ssh/authorized_keys
  echo KEY_INSTALLED"
```

**注意**：不要把口令写进命令行参数以外的地方（不写文件、不贴聊天）。装好公钥后一律走 §1.1。

### 1.3 执行约定

- **读** `production.env` 里凭据的命令用 `root` 或 `runuser -u aiops41`（文件权限是
  `0600 aiops41:aiops41`，**属主自己可读** —— 2026-09-29 实测：`runuser -u aiops41 -- test -r`
  通过。初稿这里写「`aiops41` 读不到 env 明文」，与 §1.4 的「两文件均为 `0600 aiops41`」
  自相矛盾，是错的）。
  ⚠️ **改**这些文件的动作仍按取证口径走（见本手册开头），不要因为「能读」就顺手写。
- 应用进程内执行（reconcile、ShortcutManager）用 `runuser -u aiops41 -- ...`，
  工作目录必须在 `/opt/aiops-41`（否则 `.venv` 找不到 `pyproject.toml`，报
  `PermissionError: /root/pyproject.toml`）。
- **41 上没有 `sqlite3` CLI**；查库用 `/opt/aiops-41/.venv/bin/python -c "import sqlite3; ..."`。

## 1.4 两个 env 文件的分工（2026-09-23 实测踩坑，务必先读）

41 有**两个** env 文件，用途不同，**写错文件不会报错，只会静默不生效**：

| 文件 | 谁读它 | 放什么 |
|---|---|---|
| `/etc/aiops-41/gateway.env` | systemd 的 `EnvironmentFile` | **进程环境变量**。运行时从**进程 env** 构造的配置（如 Jev 客户端的 `AIOPS_GATEWAY_JEV_*`）必须放这里 |
| `/etc/aiops-41/production.env` | 由 `AIOPS_GATEWAY_SERVER_CONFIG_FILE` 指向，`Settings.from_config` 读取 | 服务端**配置项**（模型 provider、KB、Redis 凭据等）|

**实测教训**：把 `AIOPS_GATEWAY_JEV_*` 写进 `production.env` 后重启，进程 env 里**根本看不到这些变量**，路由静默退回原模型分类器 —— 没有任何报错。写入 `gateway.env` 才生效。**改配置后务必用 `tr '\0' '\n' < /proc/<MainPID>/environ` 确认变量真的进了进程**，不要只看文件写没写。

两文件均为 `0600 aiops41`；改后保持属主与权限。

## 1.5 判别 `AIOPS_UPMS_BASE_URL` 指向的是哪个服务（只读，2026-09-28 新增）

**用途**：管家端授权链（`/user/inside/*`、`/shopuser/getShops`）依赖该地址指向
**`cloud-upms-admin`**。指向错误时上游对**任何**路径都回 `200` + 通用错误信封，
不会报 404 —— **看起来像"凭据不对"，实际是"点错了服务"**。

**执行前提（必须满足才可跑下面这段）**：

- **只读**：只请求 `GET /actuator/mappings`，它返回**路径注册表**，不含配置值与业务数据。
  **不要**读 `/actuator/env`（可能含凭据）。
- **只对 41 自己的配置目标发起**：脚本从 41 的 `production.env` 读 `AIOPS_UPMS_BASE_URL`，
  **不另外指定地址**。若该值已指向非预期服务，这**正是本步骤要发现的事实**；
  发现后**不要**继续对其它候选地址逐个试探——把结论上报，由运维决定正确地址。
- **不要在非 41 的机器上跑**：该地址只在 41 的网络位置可达。

```bash
ssh aiops-41 '/opt/aiops-41/.venv/bin/python - <<PY
import json, urllib.request, os
env = {}
for line in open("/etc/aiops-41/production.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip(chr(34))
base = env["AIOPS_UPMS_BASE_URL"].rstrip("/")

# 1) 应用上下文名 —— 直接说出这是哪个服务
with urllib.request.urlopen(base + "/actuator/mappings", timeout=15) as r:
    d = json.load(r)
print("  应用上下文:", list(d.get("contexts", {})))

# 2) AI-Ops 需要的五个路径是否存在（只看路径注册表，不读配置值）
paths = set()
def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "predicate" and isinstance(v, str): paths.add(v)
            else: walk(v)
    elif isinstance(o, list):
        for x in o: walk(x)
walk(d.get("contexts", {}))
for want in ("/user/info", "/user/ds", "/user/inside/byUserId",
             "/shopuser/getShops", "/role/list"):
    print(f"  {want:<28}", "有" if any(want in p for p in paths) else "**没有**")
PY'
```

**判据（按依赖强弱分两组，不要混用）**：

| 组 | 路径 | 谁依赖 | 缺失的后果 |
|---|---|---|---|
| **管家端必需** | `/user/inside/byUserId`、`/shopuser/getShops` | 会话身份的 C→B 映射；运营商站点范围 | **管家端授权链不可用**（本链路只调这两个） |
| **平台调用者用** | `/user/info`、`/user/ds`、`/role/list` | B 端 Bearer 调用者的范围解析（`/user/ds` 失败时降级到 `/role/list`） | 与本链路无关；只影响那条路径 |

- **必需的两个缺失** → 该地址不可用于管家端授权链。
- **只有平台组缺失** → **不影响管家端**，不要因此弃用一个可用地址。
- **五个全没有** → 该地址不是 `cloud-upms-admin`。**不要**再试凭据，
  换任何 token 都不会改变结果（上游对三种 Authorization 的响应逐字相同）。

**配置该链路的两个前提**（缺一不可，且**都尚未在 41 就绪**）：
1. `AIOPS_UPMS_BASE_URL` 指向真实可达的 `cloud-upms-admin`；
2. `AIOPS_UPMS_INSIDE_TOKEN` 有**来源**（当前全仓无出处，环境清单未登记）。
   经隧道接入时它是**回环地址**（既有 UPMS 隧道暴露 `127.0.0.1:25999`，
   见 `kb-service-test-env.md`）。

**记录**：本次判别的结论与证据见 `../validation.md` 的
「管家端端到端验收：卡在 `AIOPS_UPMS_INSIDE_TOKEN`」一节。

> **#444 起多了一条路径，它不依赖上面这两个端点**：公司 OAuth2 令牌那条（ADR-0009）直接用
> 令牌里的 `shop_ids`，校验落在公司 `/oauth/check_token`，因此**不调** `/user/inside/*` 与
> `/shopuser/getShops`。它的启用键是四项
> （`AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL` / `_CLIENT_ID` / `_CLIENT_SECRET` /
> `AIOPS_GATEWAY_COMPANY_SOURCE_KEY`），**全部缺省为空 ⇒ 与今天逐字一致**。
> ⚠️ **不要现在就配到 41 上**：第二段（路由/Nginx/绑定）没做，配了也走不到；而且来源密钥在
> 第二段之前**没有注入主体**，先配出来只是一把没人用的钥匙 —— 密钥的注入动作属于第二段，
> 见基线文档 §3.2。

## 1.6 探测公司校验入口的服务身份（**只读**，2026-09-29 新增）

**要回答的问题**：公司侧 `/auth/oauth/check_token` 对**服务间调用**是否成立 —— 即 AI-Ops 能否用
一个客户端凭据调到它。这不是「配置对不对」，是「这条路径存不存在」。

**纪律**：本节**只发请求、只读库**。**不要**把探测用的凭据写进任何 AI-Ops 配置文件 —— 见 §6 的
不变量与 #448 的显式禁令。

```bash
# ① 不带凭据（原文「无凭据返回 Full authentication is required」即出自这里）
ssh aiops-41 'curl -sS -m 8 -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" -d "token=probe" \
  https://api.mall.qushiyun.com/auth/oauth/check_token; echo'

# ② 带库里现有的客户端凭据（注意：这些 secret 就等于它们的 id，见 ③）
#    用「错的凭据」做对照更稳：见下面「判据」——无凭据与错凭据应当给出同一结果。
ssh aiops-41 'curl -sS -m 8 -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" -d "token=probe" \
  -u admin:admin https://api.mall.qushiyun.com/auth/oauth/check_token; echo'

# ③ 读 client 行（只读；确认 secret 是不是字面量）。走 §0 环境事实里那条业务库连接，
#    不要假定 41 上有 mysql CLI。
#
#    ⚠️ 四条纪律，都是评审逐轮指出来的：
#    · 口令**不要**经 shell 变量或命令行传递 —— 那会把它展开进本地与远端的进程参数
#      （`ps` 可见）。改为让脚本**自己读服务配置**：`/etc/aiops-41/production.env`
#      里有 `AIOPS_MYSQL_*`，且 `aiops41` 可读（已实测）。
#    · **用仓库自己的配置读取器**（`Settings.from_config`），不要自己拆 `KEY=VALUE` ——
#      服务读的是 `dotenv_values(..., interpolate=False)`（会处理引号），并且给未设置的
#      端口兜底 3306。自己拆会把 `"abc"` 的引号当口令、把缺失端口当错误，于是「探测失败」
#      与「服务其实连得上」分不开。传给它的必须是 `Path`，不是 `str`。
#    · 也**不要**依赖别的片段遗留的 shell 变量：每条 `ssh` 起的是独立远端 shell。
#    · 这一步只要「secret 是不是等于 id」这一个事实，因此**只打印长度与是否相等**，
#      不打印明文。即使今天的值是公开字面量，轮换之后同一条命令就会把新口令刷到终端
#      和 shell 历史里。要看明文是**一次显式的、被记录的决定**，不是默认行为。
ssh aiops-41 'runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python - <<PY
import sys
from pathlib import Path
sys.path.insert(0, "/opt/aiops-41/src")          # 线上代码；不是本仓 checkout
import pymysql
from aiops_diagnostics.config import Settings
s = Settings.from_config(Path("/etc/aiops-41/production.env"))
c = pymysql.connect(host=s.mysql.host, port=s.mysql.port, user=s.mysql.user,
                    password=s.mysql.password, database="qumall_upms")
with c.cursor() as cur:
    # 列名是 id，不是 client_id（2026-09-29 实测）
    cur.execute("SELECT id, client_secret FROM sys_oauth_client")
    for client_id, secret in cur.fetchall():
        print(client_id, "secret_len=%d" % len(secret or ""), "secret_equals_id=%s" % (secret == client_id))
PY'

# ④ 另一条入口（对照：它被图形验证码拦住）
ssh aiops-41 'curl -sS -m 8 -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" -d "grant_type=password" \
  https://api.mall.qushiyun.com/auth/oauth/token; echo'
```

**判据**（别过度解读，这是 2026-09-29 一次评审指出的边界）：①②③ 结果相同**只能**说明
「在这些输入下没有任何输入产生成功认证」。它**不能**定位被判掉发生在哪一层 —— 要区分需要
**公司侧服务端日志**，我们没有。**能据以行动的结论只有一条：服务间调用不成立**，因此
「AI-Ops 用客户端凭据调 check_token」这条路在得到公司侧答复前不要作为前提。完整记录见
`../validation.md` 的「公司校验入口对『服务间调用』是否成立」一节。

**这套步骤的收敛代价**（写下来是为了下一个人不必再走一遍）：初稿经四轮评审各修一处 ——
推断过强、口令没有来源、口令进了命令行、自己拆配置与服务读法不一致。第 ③ 步现在的形状
（脚本自读配置 + 只打印长度与相等）是这四轮的结果，**照抄即可**，不要「顺手简化」。

**结论去向**：这是**跨团队依赖**，需要公司侧/接口人答复「怎么让一个服务调用这条路径」
（建一个真正的客户端？公司另有服务间入口？）；在那之前基线 §4 第 1 条保持开放。

---

## 1.7 管家端入口：nginx 那一跳（**改前必读**，2026-09-29 实测）

### 为什么需要这一跳

AI-Ops 现在只认两种凭据：共享会话（客户端）与公司 JWT（管家端，见 #451 的本地模式 ——
⚠️ **「本地验签」这个名字只对了一半**：通道在本地，但**判据默认与公司一致、不验签**，
见本节检查单与 `operator-repair-blueprint.md` §0.1）。
`/v1/` 目前由 nginx 注入 **AI-Ops 自己的服务令牌**，所以客户端那条链路能用；
管家端拿的是**用户 JWT**，需要按入口分流。

### 正确形状是 `map`，**不是 `if`**

⚠️ `proxy_set_header` 在 `if` 块里**不合法**（实测报 `"proxy_set_header" directive is not allowed
here`）。按入口分流必须用 `map`。41 上已有现成用法：`0.websocket.conf`、`waf2monitor_data.conf`。

`map` 必须写在 **`http` 层**，而 vhost 是 `include /www/server/panel/vhost/nginx/*.conf` 进来的
（`nginx.conf:101`）—— 也就是 vhost 文件自己就在 `http` 里，可以直接在文件顶部写 `map`。
生产主配置有 `lua_package_path`，自定义 `nginx.conf` 测试时若缺它会报 `resty.core` 找不到 ——
**那是配置环境差异，不是 map 的问题**。

### 实测定稿的配置（离线起真进程验证过三种情形）

⚠️ **下面这一段要拆成两处放**：三条 `map` 在 **`http` 上下文**（vhost 文件顶部即可，
因为 vhost 是 `include .../vhost/nginx/*.conf` 进来的、本身就在 `http` 里）；而 `location` 必须
放进**已有的 `server` 块**（替换现行那个 `location ^~ /v1/`）。**不要**把整块照抄到文件顶部。

```nginx
# ① 以下三条 map 放 vhost 文件顶部（http 上下文内）
map $http_x_business_entry $aiops_entry {
    default        "consumer";
    # ⚠️ 必须容忍两侧空白：上游 `is_operator_entry` 是 `(v or "").strip().lower()`，nginx 不 strip。
    # 只写 `~*^operator$` 时，`" operator "` 会落到下面那条原样透传 ⇒ **服务令牌 + entry=operator**
    # ⇒ 上游拿服务令牌去当公司令牌验签 ⇒ 管家端登录不了（实测）。
    "~*^\s*operator\s*$" "operator";
    # ⚠️ 未知的非空值必须**原样透传**，不能落进 default 变成 consumer：
    # 上游 `PlatformIdentityResolver` 对非法入口是 403 PLATFORM_FORBIDDEN；若这里把它改写成
    # consumer，调用方会**静默落进客户端内容域**，拿不到那个 403，而 `_data_scope` 也已经在
    # 身份层按最窄的 self 收紧了 —— 现象是「错了但没人报错」。
    "~^.+$"        $http_x_business_entry;
}
# ⚠️ 服务令牌**不进 vhost**：今天它单独放在 /etc/aiops-41/nginx-aiops-service-token.conf
# （0600 root，location 内 include）。搬进 vhost 会把明文复制到第二处，且会随备份再复制一次。
# ⇒ 做法：把那份文件从「直接 proxy_set_header Authorization」改成「只设两个变量」——
#    set $aiops_service_authorization "Bearer <服务令牌>";
#    set $aiops_source_key_injected    "<来源密钥>";
#    两把钥匙因此**都不以字面量落在 vhost 里**，也不随 vhost 的备份再复制一份。
map $aiops_entry $aiops_auth {
    default    $aiops_service_authorization;   # ← 变量来自那个 0600 文件
    "operator" $http_authorization;            # 管家端：透传用户 JWT
}
map $aiops_entry $aiops_srckey {
    default    "";
    "operator" $aiops_source_key_injected;     # 同样来自那份 root-only 文件（见下）
}

# ② 以下 location 替换 server 块里现有的 location ^~ /v1/
location ^~ /v1/ {
    include /etc/aiops-41/nginx-aiops-service-token.conf;   # 现在它只 set 变量
    proxy_pass http://127.0.0.1:8788;
    proxy_http_version 1.1;
    # ⚠️ **本 location 内只允许这一条 Authorization 的 proxy_set_header**（见下）
    proxy_set_header Authorization      $aiops_auth;
    proxy_set_header X-AIOps-Source-Key $aiops_srckey;
    proxy_set_header X-Business-Entry   $aiops_entry;
    proxy_set_header X-Third-Session    $http_third_session;
    proxy_set_header Range              $http_range;
    proxy_set_header Host               $host;
    proxy_connect_timeout 15s;
    proxy_send_timeout  120s;
    proxy_read_timeout  120s;
    proxy_buffering off;
}
```

### 实测定稿的配置：五种入口值的落点

（真 nginx + 真上游回显；`Authorization` 恒发 `Bearer U.JWT`）

| `X-Business-Entry` | 上游收到 | 落点 |
|---|---|---|
| `operator` | `ENTRY=operator` / `AUTH=Bearer U.JWT` / `SRC=SRC-KEY-VALUE` | 管家端 ✅ |
| `" operator "`（两侧空格） | 同上（被 `~*^\s*operator\s*$` 规范化） | 管家端 ✅（**只写 `~*^operator$` 会落错**） |
| `OPERATOR`（大写） | 同上 | 管家端 ✅ |
| `operator-admin`（非法非空） | `ENTRY=operator-admin` / `AUTH=Bearer SERVICE-TOKEN` / `SRC=空` | **原样透传** ⇒ 上游按 `403 PLATFORM_FORBIDDEN` 拒 |
| 无该头 | `ENTRY=consumer` / `AUTH=Bearer SERVICE-TOKEN` / `SRC=空` | 客户端，**逐字不变** ✅ |

> 🟡 **空白那一行是实测踩到的**：上游做 `strip().lower()` 而 nginx 不 strip，两边不一致时该值
> **不报错**，只是落进另一条分支（服务令牌 + entry=operator ⇒ 管家端登不进去）。
> **凡上游会规范化的入参，这一跳的正则必须按同一套规范化写。**

### 🔴 为什么「两条同名 `proxy_set_header`」是一个静默用错凭据的坑（实测）

nginx 在**同一个 location 内**对同名头**不做覆盖**，而是**两条都发给上游**，顺序按指令出现顺序。
于是上游收到**两个 `Authorization` 头**；而 AI-Ops 侧（Starlette `Headers.get`）**取第一个**。

实测（真 nginx + 真上游回显 + Starlette 复现）：

| location 内写法 | 上游收到 | AI-Ops 实际读到 |
|---|---|---|
| `include`（含 set-header）+ 一条 `map` 版 set-header | `['Bearer SERVICE-TOKEN', 'Bearer USER.JWT']`（两条） | **`SERVICE-TOKEN`** ⇒ 管家端被当成客户端 |
| 同上、两条顺序调换 | `['Bearer USER.JWT', 'Bearer SERVICE-TOKEN']` | `USER.JWT`（碰巧对） |
| **只留一条 `set-header`**（令牌由 `include` 设的**变量**提供） | `['Bearer USER.JWT']`（一条） | 正确 |

**结论：保留 `include` 原样（它仍然 `proxy_set_header`）+ 再加一条 map 版 set-header，是错的** ——
它会静默地让管家端请求用服务令牌。正确形状只有一种：**让 `include` 只设变量，location 内
只留一条 Authorization 的 set-header**。

⚠️ 这个错误**不报错、不警告**，现象是「管家端请求被当成客户端」（拿到 `consumer` 内容域与本人
订单范围），排查时很难定位到「多了一个头」。

### ⚠️ 2026-09-30 两处更正（本节曾先后把这条链的判据说宽、又说偏，**两次都改了**）

**更正一：默认密钥不是「启用后跟踪的风险」，它是启用动作本身的一部分。**
原口径是「可以先启用，把换钥匙列为开放项」。**那是错的**；已把检查单第一条改成
**启用前的硬前置**（含一条可复现的相等比较命令）。

**更正二（更要紧，覆盖上面那条的前提）：这条链在「公司一致」模式下，身份判据不是验签。**
同日前端 401 的追查定案为：**公司侧既不验签也不判 `exp`**，而用户裁定 AI-Ops 按公司口径
适配（前端用户长时间停留、不重登）⇒ 生产默认 `trust_company_payload=True`，
**签名与有效期都不参与判定**。41 公网实测：把签名整段换成 `A…`、载荷逐字不变，结果
与原令牌**逐字相同**。

⇒ 因此检查单里那条「密钥不得等于默认值」**在默认模式下属于「不适用」，而不是「已满足」**
—— 两者的区别重要，写成「已满足」会让读者以为验签这道门在生效。
**默认模式下唯一的凭据门是来源密钥**（而它当前的注入主体是我们自己的 nginx，
不是设计里的「可信那一跳」）。要切回验签模式：`AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD=0`。

⇒ 运维口径随之改为：**换钥匙既不能救这条链（判据不看它），当前唯一的外部判据是
来源密钥的注入主体** —— 那正是 D 批（`operator-repair-blueprint.md` §2）要施工的东西。
在此之前**不对外宣称可用**。实测与判定过程见 `../validation.md` 与
`operator-repair-blueprint.md` §0.1。

### ✅ 2026-09-29：本节描述的配置**已在 41 落地并验收**

- nginx：`0.aiops-entry-map.conf`（三条 map）+ rewrite 里 `/v1/` 的三条 `proxy_set_header`；
  令牌文件已改为「只设变量」。
- `gateway.env`：`AIOPS_GATEWAY_COMPANY_JWT_KEY` + `AIOPS_GATEWAY_COMPANY_SOURCE_KEY` 已配，
  `CHECK_TOKEN_URL` 留空。
- 实测：客户端 200（不变）/ 管家端 `operator` 200 `count=2` / 非法入口 401 / 订单检测 `platform=operator`。
- **仍开放**：验收账号无店铺绑定 ⇒ 站点范围 Ø；客户案例 `target_agent_version: null`；
  密钥仍是公司源码默认值（启动日志每次告警）。

### 里程碑记录

本节的交付物是**一段 nginx 配置 + 一份改前检查单**，验证方式是离线起真 nginx 复刻三种入口情形
（证据表见上）。**真实故障业务验收：未完成** —— 41 上未改任何配置、通道未启用。
交付追踪：分支 `docs/nginx-operator-entry-runbook`，PR #452，base `ab3b22b`；
下一步 = 密钥换成真秘密 → 配 41 三键 → 改 vhost → 端到端验收（含客户端回归）。

### 改前检查单

- [ ] ⚠️ **判据已改为「公司一致」（不验签），所以下面这条前置在当前默认模式下属「不适用」而非「已满足」**
      —— 两者的区别重要：写成「已满足」会让读者以为验签这道门在生效。
- [ ] ⚠️ **本条只适用于严格模式**（`AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD=0`）。
      **当前生产默认是「公司一致」模式（不验签）**，该模式下密钥不是判据 —— 但它**也不能松**：
      那时唯一的凭据门是 **来源密钥**，所以「来源密钥不泄露」上升为同等重要的前置。
      要切回严格模式：设 `AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD=0` 后重启，再按下面这条判定。
- [ ] 🔴 **（严格模式）签名密钥不得等于公司源码里的默认值 —— 可一条命令判定**

      **为什么是硬前置（仅严格模式）**：**严格模式下**管家端身份唯一的判据就是验签
      （nginx 对任何 `operator` 请求都自动注入来源密钥 ⇒ 走公司令牌链 ⇒ 判身份）。
      而钥匙是公司源码里的公开默认值时，
      **任何读过那份源码的人都能自签一张自称任意运营商身份的令牌、经公网入口看该运营商的订单**。
      ⇒ **「机制可用」与「身份可信」不是同一件事**；这条前置就是把后者补上。

      **判定（不打印值、只做相等比较 —— 这是唯一能证的方式）**：

      ```bash
      # 41 上执行。$KNOWN_DEFAULT 是你**从公司源码/jar 取值后填进来**的（本仓没有它，
      # 也不该有）；取不到值就**停下**，不要判「已满足」——空值会让任何非空密钥都被判成满足。
      PID=$(systemctl show aiops-gateway-41 -p MainPID --value)
      V=$(tr '\0' '\n' < /proc/$PID/environ | grep '^AIOPS_GATEWAY_COMPANY_JWT_KEY' | cut -d= -f2-)
      if [ -z "$KNOWN_DEFAULT" ]; then echo '无法判定：未提供对照值'; exit 2; fi
      if [ -z "$V" ]; then echo '未满足：密钥未配置'; exit 1; fi
      [ "$V" = "$KNOWN_DEFAULT" ] && { echo '未满足：仍在用源码默认值'; exit 1; } || echo '已满足'
      ```

      ⚠️ **更常见的用法是「轮换后确认真的换了」，而不是「每次启用前重跑」**：既然已确认
      41 现在跑的就是默认值，日常不需要重复判定；**在换掉那把钥匙之后、以及此后每次轮换后**各跑一次，
      确认它不再等于旧值。这样这条检查**只依赖本机自有记录**，不依赖一份我们手上没有的外部源码。

      ⚠️ **千万不要用「长度告警消失了」当判据** —— 那行告警只看长度，换成另一把同样短的钥匙
      它照样出现。也**不要**用「用默认值签的令牌被拒」当判据 —— **签名正确 ≠ 令牌有效**
      （`exp`/撤销等判据仍在），那个观测证不了「钥匙不是默认值」。
      **只有「不打印值的相等比较」能证。**
- [ ] 🔴 **运维口径**：**这把钥匙的来源当前不明**（谁在何时配的没有证据），属「来源不明」而非
      「已知来源、长度不合规」。在它被换成一把**来源明确、非公开**的密钥之前，
      本路径**不应对外宣称可用**（可对内联调）。
- [ ] `gateway.env` 三键：`COMPANY_JWT_KEY` + `COMPANY_SOURCE_KEY` 有值，`COMPANY_CHECK_TOKEN_URL` **留空**（与本地模式互斥，两者都配 = 启动失败）
- [ ] `nginx -t` 通过；原 vhost 文件已备份（`cp -a ... .bak-<用途>-<时间戳>`）
- [ ] 回滚路径：**要恢复两处**，不是一处 ——
      ① `cp -a` 恢复 vhost 备份；② **恢复 `nginx-aiops-service-token.conf`**（它被改成了「只设变量」，
      旧 vhost 依赖它注入 `proxy_set_header Authorization`，只恢复 vhost 会让**客户端请求全部 401**）；
      然后 `nginx -s reload`。AI-Ops 侧清空那三个键即回到「新链路整体不启用」。
- [ ] 因此**改前必须备份两份**：vhost **与** `nginx-aiops-service-token.conf`（后者也要 `cp -a` 留档，
      否则回滚时只能凭记忆改回 set-header）
- [ ] 验收含**客户端回归**（无入口头那条必须仍是服务令牌）

## 1.8 公司网关（cloud-gateway）那一跳（**D-3/D-4 之后**，2026-09-30）

**背景**：`operator` 那条链自 2026-09-30 起**先经过公司网关**（ADR-0009 第二段）。
nginx 不再为 operator 注入任何凭据；来源密钥由**网关**覆盖式注入。

### 现状（一句话）

```
operator → nginx(/v1/) → 127.0.0.1:30899（宿主 → cloud-gateway）→ 172.18.0.1:8788（AI-Ops）
consumer → nginx(/v1/) → 172.18.0.1:8788（直连，逐字不变）
```

### 改向脚本（改前必读）

执行脚本在仓库的 `deploy/d4-cutover.py`。**41 上没有仓库 checkout**，所以要先按仓库
一贯的取证口径把它送上去（传文件 + 核对 sha，与 §2 部署同一套纪律）：

```bash
# 本地（仓库内）——记下 sha
sha256sum deploy/d4-cutover.py

# 送上 41
scp deploy/d4-cutover.py aiops-41:/tmp/d4-cutover.py

# 41 上——**核对 sha 与本地一致再执行**
sha256sum /tmp/d4-cutover.py     # 与上面那个值逐字相同才继续
python3 /tmp/d4-cutover.py dry-run    # 只打印将写入的内容
python3 /tmp/d4-cutover.py apply      # 备份 → 写 vhost+map → nginx -t → reload
python3 /tmp/d4-cutover.py rollback   # 从最近一次备份恢复（**两处**）→ reload
```

⚠️ 脚本**不进 `REFERENCE_FILES`**（那是 CD 同步的运行时参考资料），所以**不会被 CD 送到 41** ——
每次都要手工传。这正是上面这几行的存在理由，不是多余的步骤。

> **顺带说明（评审指出，2026-10-01 修正；同日 #520 收窄 `paths` 后已闭合）**：
> 本条原写「脚本放进 `deploy/` 会命中 `paths`，那是可接受的」——**已被 #405 的评估推翻**：
> 不是幂等的问题，而是**重启会重置观察窗、并打断在飞诊断**。实测：只改只读脚本的 PR
> 已这样重启过生产三次。见 `validation.md` 的「#405 到期评估」。
>
> **#520 已把 `push.paths` 从 `deploy/**` 收窄到 deploy-41.sh 真正执行的两个文件**
> （`deploy-41.sh`、`classify-remote-result.sh`），所以现在改 `d4-cutover.py`、
> `company-gitlab-api.sh`、`routing-window.sh` 这类文件**不再触发部署**，上面那句
> 「改 `deploy/` 下的任何文件都要先问一句」对它们不再成立。
> 反过来，**改那三个只读脚本时不再有任何自动提示** —— 它们本来就靠人手工传上去。

- 备份落 `/var/backups/aiops-41/d4-cutover-<时间戳>/`（vhost、map、tokenconf 各一份）；
- **回滚必须恢复两处**：`api.mall.qushiyun.com.conf` **与** `0.aiops-entry-map.conf`。
  只恢复 vhost 会引用不存在的 `$aiops_upstream`，`nginx -t` 直接失败。
  `nginx-aiops-service-token.conf` **不需要**跟着回滚（保持「只 set 变量」两种形态都可用）。

### 🔴 Nacos 那边：加完路由**必须重启网关容器**（实测，别被日志骗）

`DynamicRouteInit` 的监听器**会**打印「加载路由：<id>」并保存，但**新建的那条不生效** ——
同一批里既有路由正常响应、只有新路由 404；重新发布同样内容也无效。
（线上 jar 里**不存在** `RefreshRoutesEvent`，刷新根本没被触发。）

```bash
# 改 Nacos（dataId=dynamic_routes / group=DEFAULT_GROUP / public）
# 然后：
docker restart cloud-gateway && sleep 25
docker logs cloud-gateway --since 2m | grep "加载路由：aiops-gateway"
```

### 那条路由为什么必须带 `RewritePath`

本网关**会剥掉匹配到的那段前缀**（既有 35 条全都用 `RewritePath` 把它加回来）。
少了 `RewritePath=/(?<segment>.*),/v1/$\{segment}`，AI-Ops 收到的路径是
`/faq/recommendations`（少 `/v1`）⇒ `404`。

### 安全口径（**别读成"有网关兜底"**）

- 网关**不做身份强制**（`cloud.auth.enable: false`），也**不注入身份**；
- `30899` 这条网关端口**公网可达**（实测 `http://47.97.160.153:30899/upms/user/check` → `200`）；
- ⇒ 那条路径上的防线是 **AI-Ops 自己的令牌校验**，而它**不验签、不判 `exp`**。
- **D 的收益是「注入主体从我们自己的 nginx 换成网关」，不是「身份由公司校验」。**
  暴露面发现见 `fleet-ops/FLEET.md` §7.7（私有记录），**不是我们该单方面改的**。

---

## 1.9 入口流量怎么取数（**只读**，2026-10-01 新增）

回答「**这个窗口里到底有没有人用过 AI-Ops**」。两类证据要**一起看**，因为它们各自有盲区。

### 证据一：网关自己的指标表（`agent_run_metrics`）

```bash
deploy/routing-window.sh                     # 窗口 = 当前进程启动时刻
deploy/routing-window.sh 2026-10-01T00:00:00+00:00   # 或指定 UTC 起点
```

⚠️ **两张表都空 ≠ 没有流量。** `/health` 与媒体路由**不写指标行**，所以空窗口
只能推出「没有到达这张表的请求」。要断言入口侧没人来过，得看证据二。

### 证据二：nginx 入口访问日志

```bash
# 今天全天（**主机本地**时刻 00:00 起）有多少条打到 AI-Ops 的 /v1/
# 日期在 41 上现算 —— 不要手写日期，否则第二天照抄会去数昨天。
ssh aiops-41 'd=$(LC_ALL=C date "+%d/%b/%Y"); \
  sudo -n grep "$d" /www/wwwlogs/api.mall.qushiyun.com.log | grep -c "/v1/"'

# 只看真实的业务路径（把它们与扫描流量分开）
ssh aiops-41 'd=$(LC_ALL=C date "+%d/%b/%Y"); \
  sudo -n grep "$d" /www/wwwlogs/api.mall.qushiyun.com.log \
  | grep "/v1/" | awk "{print \$4, \$7, \$9}"'
```

⚠️ **日期必须现算，不能写死。** nginx 的 `$time_local` 形如 `01/Oct/2026:08:57:16`
（英文月份缩写），所以用 `LC_ALL=C date "+%d/%b/%Y"` 生成；写死一个日期的话，
**换一天再跑，数到的还是那一天的行** —— 而这个查询的整个意思是「**今天**」。
查历史日期时另给参数，并注意日志可能已轮转出当前文件（那时会得到 0，**不代表没人来过**）。

> ⚠️ **本节的两条命令是从这份文件里原样复制出来跑过的**（不是"手打一版差不多的"）。
> 原因：写这两条时我把 `grep "\$d"` 写进了文件（多了一个反斜杠），
> 而**验证时手打的是 `grep "$d"`** —— 于是「验过了」与「文件里那一条」是两条不同的命令，
> 文件里那条会去匹配字面量 `$d`、永远返回 0。**验命令必须从文档复制，不能凭记忆重打。**

- 日志路径：`/www/wwwlogs/api.mall.qushiyun.com.log`（`api.mall.qushiyun.com` 的 vhost
  与 `/v1/` 分流见 §1.7）；
- **该文件很大**（每天数 GB），**别不加日期就 `grep`** —— 全表扫一次要几分钟，
  而且会盖住「今天」这个口径；
- **`.env` / `wp-admin` 这类 404 是扫描器**，不是业务流量，计数时要能分辨。

### 两个口径别混

| 问的问题 | 起点 | 用哪个 |
|---|---|---|
| 「**本次部署之后**判定路径被用过吗」 | 进程启动时刻 | `routing-window.sh`（默认） |
| 「**今天**有没有人到过 AI-Ops」 | 主机本地 00:00 | 证据二的 `grep '<日期>'` |

⚠️ 日志是**滚动追加**的：当天没结束就取数，读到的是**到取数时刻为止**的读数，
不是「一整天」的终值。写结论时把时刻一起写出来。

---

## 1.10 Dify 的那一跳：`location ^~ /v1/dify/`（**2026-10-09 落地并验收**）

### 为什么单开一条 location

`location ^~ /v1/` 用 AI-Ops 自己的服务令牌**覆盖** `Authorization`
（`proxy_set_header Authorization $aiops_auth;`）。而 Dify 的 External Knowledge API
**只支持 Bearer 自带 key** —— 它的 key 被覆盖掉，适配路由收到的是我们的服务令牌，
回 **403 `DIFY_CREDENTIAL_REJECTED`**（2026-10-09 公网实测）。这不是缺陷：
`/v1/` 那条是按"nginx 供凭据"设计的，而 Dify 是**服务器、不是我们的用户**，
它没有会话也没有 UPMS 主体。

### 形状（已由 `deploy/d4-cutover.py` 生成，不要手改）

```nginx
location ^~ /v1/dify/ {
    proxy_pass http://172.18.0.1:8788;
    rewrite ^/v1/dify/(.*)$ /v1/dify/$1 break;
    proxy_http_version 1.1;
    proxy_set_header Authorization      $http_authorization;   # ← 保留 Dify 的 key
    proxy_set_header Host               $host;
    proxy_set_header X-Real-IP          $remote_addr;
    proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_connect_timeout 15s;
    proxy_send_timeout  120s;
    proxy_read_timeout  120s;
    proxy_buffering off;
}
```

三处**刻意**与 `/v1/` 那条不同，读的人不要"顺手对齐"：

- **不** `include /etc/aiops-41/nginx-aiops-service-token.conf`（那是调用者链的凭据）；
- **不**注入 `X-AIOps-Source-Key` / `X-Business-Entry` / `X-Third-Session` ——
  注入它们会让下游以为这是某个真实用户发来的请求，而这条路由的前提正是"它不是"；
- 上游**固定**直连 AI-Ops，不进 `$aiops_upstream` 的 map —— Dify 没有"入口"可言。

**为什么它排在 `/v1/` 之前**：nginx 按**最长前缀优先**选 location，与书写顺序无关；
`/v1/dify/` 比 `/v1/` 长，所以它赢。写在前面只是为了可读 —— 别把"顺序"当成判据。

### 回滚

```bash
ssh aiops-41 'python3 /tmp/d4-cutover.py rollback'    # 从最近一次 d4-cutover-* 备份恢复 vhost + map
```

`apply` 会在写之前备份 vhost、map、token conf 三份到
`/var/backups/aiops-41/d4-cutover-<ts>/`；`nginx -t` 失败会**自动回滚**。

### 验收（2026-10-09 实测，三处观察点）

| 观察点 | 无凭据 | 假 key | 真 key |
|---|---|---|---|
| 从开发机打公网 `https://api.mall.qushiyun.com/v1/dify/retrieval` | 401 `DIFY_CREDENTIAL_REQUIRED` | 403 `DIFY_CREDENTIAL_REJECTED` | — |
| 从 41 自身打**同一个公网 URL** | — | — | **200 + `{"records":[…]}`（2 条）** |
| 从 **Dify 容器**（`deploy-api-1`）经 `ssrf_proxy` 打 | 401 | 403 | —（key 在 41 的 0600 文件里，探针读不到） |

**这三行合起来才是"Dify 打得通"**：第一行证明 key 真的到了门（改动前无凭据也是 403，
因为 nginx 塞的是我们的服务令牌）；第二行是**与 Dify 完全相同的 URL 形状 + 真 key → 200**；
第三行证明**从 Dify 自己的网络位置**这条 URL 可达且门生效。真 key 那一格没在容器里跑，
是因为凭据不该进容器 —— 那是有意的边界，不是遗漏。

## 2. 部署（源码同步到 41）

生产代码是文件拷贝部署（41 无 `.git`）。流程：**备份 → 传 → 校验 sha → 重启**。

> **常规部署走 CD，不按本文手敲。** 合并到 `main` 且改动含 `src/**` 时，
> `.github/workflows/cd.yml` 会自动准备好一次部署，经 `production-41` 环境的
> 执行 `deploy/deploy-41.sh`（2026-09-30 起**不再有人工审批**，见下方决定记录）。
> **自动化路径与手工路径共用同一个脚本**，
> 所以两者不会各自漂移 —— 本节保留手工步骤是为了：审阅这个脚本、以及自动化挂掉时
> 的应急路径。
>
> **什么时候仍需手工**：CD 不可用（runner 掉线、GitHub 故障）。手工执行即
> `deploy/deploy-41.sh --commit <sha>`（或 `--rollback-to <sha>`），用人工密钥
> 别名 `aiops-41`；自动化用的是 CD 专用别名 `aiops-41-cd`。
>
> 🔴 **手工部署必须留痕**（#407，`deploy/record-manual-deploy.sh`）。这不是流程洁癖：
> 一次 CD 连续 6 次没成功部署而无人察觉，正是因为**同期的确有人在手工 rsync**
> —— 生产确实更新了，于是没有任何理由去看 CD。**不可见的人工动作会连带隐藏本应被
> 发现的异常。** 部署后当天追加一行：
> `deploy/record-manual-deploy.sh <commit> "<为什么走手工>"`，
> 记录落在 [`deploy/manual-deploys.md`](../../deploy/manual-deploys.md)（并同步到 41 的
> 运维记录）。**先部署后补记也行，但不能不记。**
>
> 🛑 **谁负责批准 CD —— 2026-09-30 起：没有人，门已去掉。**
> 仓库所有者的明确决定（原文：「我授权你可以自动的进行 CD 审批部署，不需要我认为的点击」，
> 经追问确认取**永久去掉批准门**）：`production-41` 环境的 `required_reviewers` 已移除，
> **且 `cd.yml` 里没有任何"自动批准"的代码**（见下：那样写做不到）。
>
> **含义**：**任何合并到 main 且命中 `cd.yml` 的 `paths` 的提交，都会自动部署到生产。**
> 「什么人能改生产」因此等于「**什么人能合并到 main**」—— 那道门仍在（Ruleset 要求 PR、
> 必需检查、会话解决），去掉的只是"合并后再点一次"。
>
> ⚠️ **回滚**：在 GitHub 的 `production-41` 环境上重新加回 required reviewer 即可，
> **不需要动 `cd.yml`**（那里做不出自动批准，所以也不存在"假门"这个风险）。
>
> ⚠️ **仍然不要因为等待就手工部署**：现在根本没有"等待"这个状态了。若 CD 没跑，
> 那是路径没命中或 workflow 出错，查 `gh run list --workflow=cd.yml`，不要绕过去手敲。
> 卡住/连续失败会由 `CD Watch` 开 `cd:needs-attention` 票（#407）。
>
> 🔴 **反向：若你把 D-2 的暴露回滚了（服务绑回 `127.0.0.1`），必须同时把
> `deploy-41.sh` 的健康检查改回回环**（`AIOPS_HEALTH_HOST=127.0.0.1` 或直接改默认值）——
> 否则它会拿网桥地址去查一个只监听回环的服务，**每次部署都自检失败并回滚**，
> 而且回滚后的"服务是否恢复"也会被误报为失败（评审指出）。这条与正向的
> 「绑定 + nginx 上游必须一起改」是同一个道理的第三面：**地址这件事牵动服务、nginx、
> 部署脚本三处。**
>
> **依赖不在本流程范围。** `src/` 与运行时参考资料由 CD 同步；依赖清单
> （`pyproject.toml` / `uv.lock`）不一致时 CD 会**拒绝部署**。更新 41 的依赖环境走
> [依赖环境更新流程](env-41-dependency-update.md) —— 那是一次人工的、要留记录的
> 环境变更，不做成自动化。
>
> `deploy-41.sh` 在本文步骤之上多了两件**唯一**有它才有的能力：把 commit 标识注入
> `__init__.py`（使 `/health` 能自证跑的是哪个 commit），以及部署后逐条断言
> （服务 active、`/health` 含该 commit、文件数与 sha 树一致）。下面的手工步骤没有
> 这些断言 —— 这正是「手工路径会漂移」的具体表现，也是保留脚本的理由。

> **打包与 rsync 源必须成对**（本节唯一容易错的地方）。`-C src aiops_diagnostics`
> 让包内首层就是 `aiops_diagnostics/`，所以解包后的 rsync 源是
> `/tmp/sync-check/aiops_diagnostics/`。若改成 `tar czf x src/aiops_diagnostics/`
> （不带 `-C`），首层会变成 `src/`，源就必须写成
> `/tmp/sync-check/src/aiops_diagnostics/`——同理，用 `--strip-components` 会剥掉
> 目录层把文件散到 `src/` 根。**打包后先 `tar tzf` 看一眼首层，再决定 rsync 源。**

```bash
# 1) 本地打包（在 canonical checkout，确保在目标 commit）
tar -czf /tmp/aiops-sync.tar.gz -C src aiops_diagnostics
first=$(tar tzf /tmp/aiops-sync.tar.gz | head -1)
[ "$first" = "aiops_diagnostics/" ] || { echo "错误：包内首层是 $first，期望 aiops_diagnostics/；先停下，别上传" >&2; exit 1; }

# 2) 上传
scp /tmp/aiops-sync.tar.gz aiops-41:/tmp/

# 3) 在 41 上：先备份，再解到临时目录核对，最后 rsync 覆盖
ssh aiops-41 '
set -e
mkdir -p /var/backups/aiops-41/backup-$(date +%Y%m%d-%H%M%S)
cp -a /opt/aiops-41/src /var/backups/aiops-41/backup-$(date +%Y%m%d-%H%M%S)/
# 先清空再解包。解包目录若留着上次中断部署的残留，rsync 会把它们当成本次内容
# 一起同步过去——--delete 只删目标侧多出的文件，不管源侧多出的。
rm -rf /tmp/sync-check && mkdir -p /tmp/sync-check
tar xzf /tmp/aiops-sync.tar.gz -C /tmp/sync-check
rsync -a --delete /tmp/sync-check/aiops_diagnostics/ /opt/aiops-41/src/aiops_diagnostics/
chown -R aiops41:aiops41 /opt/aiops-41/src
systemctl restart aiops-gateway-41.service && sleep 5
systemctl is-active aiops-gateway-41.service
curl -s --max-time 6 http://172.18.0.1:8788/health
rm -rf /tmp/sync-check /tmp/aiops-sync.tar.gz'
```

上面第 3 步的 `rm -rf` 不是保守起见，是**必需**的：`rsync -a --delete` 只删除
**目标**目录里多出的文件，**不会**删除**源**目录里多出的文件。若 `/tmp/sync-check`
残留着上次中断部署解开的文件，它们会作为本次内容被同步到生产。部署后核对文件数
（与本地 `git ls-files src/aiops_diagnostics/ | wc -l` 对比）能发现这类污染。

**逐文件 sha 校验（必做）**：

```bash
# 本地
for f in $(git ls-files src/aiops_diagnostics/); do echo "$f $(sha256sum "$f" | cut -c1-16)"; done | sort > /tmp/local.txt
# 41（排除 __pycache__）
ssh aiops-41 'cd /opt/aiops-41 && for f in $(find src/aiops_diagnostics -type f -not -path "*__pycache__*" | sort); do echo "$f $(sha256sum "$f" | cut -c1-16)"; done' | sort > /tmp/remote.txt
diff /tmp/local.txt /tmp/remote.txt && echo "41 == main"
```

回滚。**注意源路径必须以 `backup-<时间戳>/src/` 结尾**——上面第 3 步的备份做的是
`cp -a /opt/aiops-41/src $B/`，所以备份内是 `<时间戳>/src/`，且同级还有一个 `gateway.db`。
若照旧写成 `backup-<时间戳>/`（漏掉 `/src/`），rsync 会把它们**塞进**
`/opt/aiops-41/src/src/`，并把 `gateway.db` 撒进 `src/`：

```bash
ssh aiops-41 '
set -e
rsync -a --delete /var/backups/aiops-41/backup-<时间戳>/src/ /opt/aiops-41/src/
chown -R aiops41:aiops41 /opt/aiops-41/src
systemctl restart aiops-gateway-41.service && sleep 5
systemctl is-active aiops-gateway-41.service
curl -s --max-time 6 http://172.18.0.1:8788/health'
```

回滚前建议先干跑确认路径正确（应**无**源码差异，只可能有 `__pycache__` 时间戳差异）：

```bash
ssh aiops-41 'rsync -an --delete --itemize-changes \
  /var/backups/aiops-41/backup-<时间戳>/src/aiops_diagnostics/ \
  /opt/aiops-41/src/aiops_diagnostics/'
```

## 3. 资源创建（走生产代码路径，禁止手工插库）

### 3.1 Agent（清单驱动 reconcile）

清单追加 `[[agents]]`（字段见 `ops/README.md`）后：

清单有**两份、内容不同**，先确认拿哪一份：

| 文件 | 是什么 |
|---|---|
| `/opt/aiops-41/ops/environments/env-41.toml` | **已漂移**（3 个 agent 是 `qwen3.8-max-0902`、两个 KB id 是旧的）。拿它跑会在**任何写入之前**中止，实测退出码 2、库零变更：`收敛中止（库未变更）: 模型不在白名单 … 允许: deepseek-v4-flash` |
| `/opt/aiops-41/ops/environments/env-41.toml.new` | 上次的人工放置副本，**可能也滞后**于仓库 |
| **仓库 `ops/environments/env-41.toml`** | 权威（库与它一致） |

⇒ **别用部署目录那两份**：把它们拷到别处、用**仓库那份**跑（或先把仓库那份 `install` 成
`env-41.toml.new` 再跑）。`reconcile` **只从你给的那个路径**读清单，不会自己去部署目录找。

```bash
# 在 41 上，把仓库那份（如 scp 到 /tmp/env-41.toml）装成一个明确的工作副本
install -o aiops41 -g aiops41 -m 0640 /tmp/env-41.toml /opt/aiops-41/ops/environments/env-41.toml.new
cd /opt/aiops-41
runuser -u aiops41 -- .venv/bin/aiops --config /etc/aiops-41/production.env admin reconcile \
  ops/environments/env-41.toml.new \
  --db /var/lib/aiops-41/gateway/gateway.db \
  --kb-url http://127.0.0.1:29380 --dry-run
```

- `--dry-run` 先看动作（`created`/`unchanged`/`registered`），确认后去掉 `--dry-run` 实跑。
  **dry-run 之后回查一次库**（例如 `dify_app_registry` 的行数仍为 0），否则"没写"只是假设。
- **幂等验收**：连续实跑第二次应全部 `unchanged`。
- `--db` 必须显式给（不 source env 时 `from_env()` 回退 XDG 会**静默建空库**）。
- **`[[dify_apps]]` 是 fail-closed 的**：表里出现**任何一行**之后，未登记的
  `(租户, 入口)` 就什么都不选（`unavailable`）。所以"加一行"是整台主机的一次开关 ——
  会把**谁**的答案改成什么，先跑 `tools/dify_registry_blast_radius.py` 量出来再动。
- 发布前 KB 活性校验会真实调用 kb-service；供应商欠费时（embedding 502）会误报
  "知识库不存在"，**这是误报**，充值后重试即恢复，不要据此删绑定。

### 3.1-bis 把动作发布为**平台默认**（2026-09-28 新增）

**用途**：让**所有租户**的某个入口都能看到该动作（consumer 侧现有的 4 条平台默认
就是这么来的）。**按租户发布**见下一节 3.2。

**改前必备份。** `ShortcutStore` 用 WAL：**只 `cp` 主库、不停服务**会得到一个不含
未检查点提交的副本 —— 看着有、回滚时才发现少了数据。所以这套命令有四个硬要求：
`set -e`（任一步失败就停）、**确认服务真的停了再复制**、**复制后校验副本可打开**、
`trap` 保证无论怎么退出都**把服务拉回来**。备份没成功就**不要往下发布**。

```bash
ssh aiops-41 'set -eu
TS=$(date +%Y%m%d-%H%M%S); B=/var/backups/aiops-41/shortcuts-$TS; mkdir -p "$B"
# 退出时无条件尝试恢复服务；**并把启动结果计入退出码** —— 备份成功但服务没起来
# 不算成功（值班人员不能从"备份就绪"里看出网关还躺着）。
restore() {
  systemctl start aiops-gateway-41.service >/dev/null 2>&1 || true
  if systemctl is-active --quiet aiops-gateway-41.service; then
    echo "网关已恢复运行"
  else
    echo "!! 网关未能启动 —— 立即人工介入（备份本身可能仍是好的）" >&2
    exit 1
  fi
}
trap restore EXIT
systemctl stop aiops-gateway-41.service
sleep 2
if systemctl is-active --quiet aiops-gateway-41.service; then echo "服务未停止，放弃"; exit 1; fi
cp -a /var/lib/aiops-41/gateway/gateway.db "$B/gateway.db"
test -s "$B/gateway.db"
head -c 16 "$B/gateway.db" | grep -q "SQLite format 3" || { echo "副本不是 SQLite 文件，放弃"; exit 1; }
echo "备份就绪: $B"'
```

**判据**（三条同时成立才算通过；**不要**写成"最后一行必须是什么"——`EXIT` trap 会在
`备份就绪` 之后再打印一行 `网关已恢复运行`，末行判据会把成功当失败）：

1. **退出码为 0**；
2. 输出里出现 `备份就绪: <路径>`；
3. 输出里出现 `网关已恢复运行`（没有它、或出现 `!! 网关未能启动` → 停止）。

**再加一条表数校验**（确认副本结构完整，而不只是"有个文件"）—— 用 heredoc 写，
避免单引号里嵌套引号：

```bash
ssh aiops-41 '/opt/aiops-41/.venv/bin/python - <<PY
import sqlite3, sys
c = sqlite3.connect("/var/backups/aiops-41/shortcuts-<TS>/gateway.db")
n = len(c.execute("select name from sqlite_master where type=\"table\"").fetchall())
print("表数 =", n)
sys.exit(0 if n > 0 else 1)
PY'
```

判据：**表数 > 0**。为 0 说明副本是空库，回滚不了 —— 同样**停止发布**。

**服务状态说明（易误读）**：上面这段以 `trap ... EXIT` 收尾，**备份成功失败都会尝试
把网关拉回来**；并且**启动失败会让整条命令返回非零** —— "备份好了但服务没起来"不是
成功。所以命令结束时正常情况是**输出里有 `备份就绪` 且有 `网关已恢复运行`、退出码 0**；
若只看到 `!! 网关未能启动`，先修服务、**不要**继续发布。

（备份只占几秒，因此让服务多停这一会儿是可接受的；`ShortcutManager` 写库时服务本来
就在跑，所以**发布步骤不需要**为它额外停服务。）
紧接着的**发布脚本是在服务运行状态下写库的**，这与 `ShortcutManager` 日常被 HTTP
接口调用时完全一样（它自己开连接、自己提交），因此**不需要**为发布额外停服务。
（本页初稿没写这句，读者容易以为"备份完还得手动起服务"。）

**发布**（走生产生命周期，**不要**直接 INSERT）：

> ⚠️ **信任边界，必须先读。** 下面这段在**主机上构造** `ROLE_PLATFORM_ADMIN` 上下文。
> `ShortcutManager` 校验的是**传入的 context**，不是"谁在敲命令" —— 所以**能执行本脚本
> 的人就能以平台身份发布对所有租户可见的动作**。这是**有意的设计边界**：在这台主机上
> `root`/`aiops41` 的 shell 访问本身就是最高信任级，平台发布属其射程。
> **但要说清两点**：① 这条路径**不是**面向人的自助入口，它是**运维动作**，执行记录要留
> （谁、什么时候、发了什么）；② 面向人的平台发布入口是**带鉴权的 HTTP 管理接口**
> （`ROLE_PLATFORM_ADMIN` 由 UPMS 角色解析得出），主机脚本是它的**旁路**，
> 仅用于运维与首次发布。**不要把这条脚本当成"平台管理员登录"。**

> **两条路径不是"等效的两种做法"，差别就在身份从哪来。** HTTP 接口同样支持
> `scope=platform`（`create_shortcut` 的 `scope` 查询参数，走**同一个**
> `ShortcutManager.create`），但它要求 `caller` 的 `roles` 含 `ROLE_PLATFORM_ADMIN`。
>
> ⚠️ **而 41 上这条 HTTP 路径当前结构上不可达，不只是"缺个令牌"**：
> `_caller_resolver` 在配置了 `AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN` 时
> **只选** `RedisThirdSessionResolver`（`gateway_api.py`）—— 那一级 `return` 掉之后不再往下看，
> 而它会话解析出来的 `roles` **恒为空集**。也就是说 41 上无论用谁的令牌，管理面都拿不到平台角色。
> （#443 新增的公司令牌路径曾排在那一级之后，因此也够不到 —— **#444 已把它改接在会话那一级
> 之前**，但那只是把门装上：第二段的路由与 Nginx 未做、来源密钥也还没有注入主体，所以管家端
> 令牌在 41 上**仍然不通**。管理面要复用的正是这同一处「接在会话之前」的接法。）
> ⚠️ **本手册的管理面脚本不受这条影响**：它走的是主机上的旁路，不是 HTTP 面。
> 要走通 HTTP，需要**另外一层身份路由**（例如给管理面单独一个解析器，或在会话解析里
> 补角色）—— 那是**独立改动**，不在本手册射程内。这也与管理面 HTTP 化受阻同源
> （`kb-service-test-env.md` 记载 `ROLE_AGENT_ADMIN` 角色族未建）。
>
> **因此主机脚本是一条临时旁路，不是终态**，且它的存在**有结构性理由**：
> 管理面的 HTTP 身份路径当前不通。平台侧补齐角色族**并**给管理面一条能解析角色的
> 身份路由之后，首次发布应改走 HTTP；主机脚本退化为断网/应急手段。

```text
# 脚本放 /tmp 并用 644（runuser 读不到 /root）；cd /opt/aiops-41
sudo install -m 644 /root/pub.py /tmp/pub.py
cd /opt/aiops-41 && runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python /tmp/pub.py
```

```python
from pathlib import Path
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import (
    PLATFORM_SCOPE, PLATFORM_TENANT_ID, ShortcutManager, ShortcutStore,
)
store = ShortcutStore(Path("/var/lib/aiops-41/gateway/gateway.db"))
manager = ShortcutManager(store)
subject = SubjectRecord(b_user_id="B-onbox-admin", tenant_id=PLATFORM_TENANT_ID)
ctx = ScopeContext.build(
    caller=subject, subject=subject, delegated=False,
    effective_tenant_id=PLATFORM_TENANT_ID, data_scope=DataScope(type="self"),
    roles=frozenset({"ROLE_PLATFORM_ADMIN"}),          # 必需：缺它会被 ShortcutForbidden 拒
    permissions=frozenset({"aiops:shortcuts:manage"}),
)
row = manager.create(ctx, {"business_entry": "operator", "code": "smart_diagnosis", ...},
                     scope=PLATFORM_SCOPE)
cur = store.get(row.shortcut_id, PLATFORM_TENANT_ID)
if cur.status == "draft":
    manager.publish(ctx, row.shortcut_id, expected_revision=cur.revision, scope=PLATFORM_SCOPE)
```

**幂等重跑（发布中断时的恢复）**：这段脚本**不是**天然幂等的 —— 首次 `create` 成功后
若 `publish` 之前中断，再跑会在唯一键唯一约束处失败。按下面三种状态分别处理：

| 现状 | 动作 |
|---|---|
| 平台行**不存在** | 正常流程：`create` → `publish` |
| 存在且 `status=draft` | **不要**再 create；直接 `publish(expected_revision=当前 revision)` |
| 存在且 `status=published` | 已完成，跳过（要改内容则 `fork_draft` → `update` → `publish`） |

查现状（只读）：`manager.list(ctx, scope=PLATFORM_SCOPE, business_entry="<entry>")`。

**验收**：`GET /v1/shortcuts` 带目标入口头，看 `count` 与 `codes`。

**常见坑**：
- `admin migrate-shortcuts` **不能**用来首次建平台默认 —— 它是**从已发布的租户行复制**
  的；某入口若一条已发布租户行都没有，它会跳过（这就是 `operator` 侧长期为 0 行的原因）。
- `create` 之后拿到的是**草稿**，必须再 `publish`；只 create 不 publish 入口看不到。

### 3.2 快捷动作（ShortcutManager，无 CLI）

`admin` CLI 没有 shortcut 子命令；用生产类在 41 上创建，**不要**直接 `INSERT INTO shortcuts`：

```text
# runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python 执行
from pathlib import Path
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

store = ShortcutStore(Path("/var/lib/aiops-41/gateway/gateway.db"))  # 必须 Path，不是 str
manager = ShortcutManager(store)
subject = SubjectRecord(b_user_id="B-admin-41", tenant_id="<租户>")
admin = ScopeContext.build(
    caller=subject, subject=subject, delegated=False,
    effective_tenant_id="<租户>", data_scope=DataScope(type="self"),
    roles=frozenset({"ROLE_AGENT_ADMIN"}),
    permissions=frozenset({"aiops:shortcuts:manage"}),
)
s = manager.create(admin, {
    "business_entry": "consumer", "code": "case_exploration", "intent": "case_exploration",
    "requires_order": False, "sort_order": 10,
    "labels": {"zh": "客户案例", "en": "Customer Cases"},
    "descriptions": {"zh": "...", "en": "..."},
    "question_templates": {"zh": "我想看看客户案例", "en": "..."},
    "target_agent_version": "agt_xxx#v1",   # 宣传类才填，其余 None
})
manager.publish(admin, s.shortcut_id, expected_revision=s.revision)
```

创建后**必须**用公网 `GET /v1/shortcuts` 验收（见 §5）。

**改造一条已发布动作为跳转动作**（不新增行，`jump_path` 非空即跳转）：

```text
# 派生草稿 → 改 jump_path → 发布。旧发布版本快照保留，可 rollback。
draft = manager.fork_draft(admin, shortcut_id, expected_revision=current.revision)
manager.update(admin, draft.shortcut_id, {
    "expected_revision": draft.revision,
    "intent": "report_fault", "requires_order": False, "sort_order": 30,
    "labels": {"zh": "故障上报", "en": "Report a Fault"},
    "descriptions": {…}, "question_templates": {…},   # 保留原值
    "jump_path": "/charge/pages/faultReport/faultReportList",
})
manager.publish(admin, updated.shortcut_id, expected_revision=updated.revision)
```

- `jump_path` 必须 `/` 开头、≤512 字符；留空/省略即提示动作。
- **平台默认行与租户行都要改**：`list_effective` 中租户已发布行覆盖平台默认行，只改一条会分叉。
- 改前先做精确 SQLite 备份（见 §5 的备份纪律）。

## 4. 真实会话与数据（验收前置）

### 4.1 取有效 thirdSession

会话键在公司会话 Redis，41 本机 `127.0.0.1:6379`，前缀 `app:3rd_session:`：

```bash
ssh aiops-41 '
PW=$(grep -oE "^AIOPS_REDIS_PASSWORD=.*" /etc/aiops-41/production.env | cut -d= -f2)
for K in $(redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning --scan --pattern "app:3rd_session:*"); do
  V=$(redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning get "$K" | tr -d "\000-\010")
  TEN=$(echo "$V" | grep -oE "\"tenantId\":\"[0-9]+\"" | head -1)
  TTL=$(redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning ttl "$K")
  echo "$K | $TEN | ttl=$TTL"
done'
```

- **thirdSession 令牌 = 键名去掉 `app:3rd_session:` 前缀后的部分**。
- **请求头 `tenant-id` 不能覆盖会话身份**：以会话 `tenantId` 为准，租户必须匹配，
  否则拿不到该租户的数据（实测：某演示会话实际解析为另一租户）。
- 先冒烟验证再使用：`GET /v1/faq/recommendations` 返回 200 即有效。
- 会话是业务方提供的敏感凭据：**不落仓库、不贴文档、不写聊天记录**。

### 4.2 找该会话可归属的真实订单

订单归属校验查 `ch_order_info`（**不是** `ch_occupy_order_info`），列为
`order_no / user_id / tenant_id`：

```text
# runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python 执行（口令从 env 现场取）
docker_mysql = dict(host="192.168.1.45", port=3306, user="mall",
                    password=os.environ["PW"], database="cloud_charging_pile")
cur.execute("SELECT order_no FROM ch_order_info WHERE tenant_id=%s AND user_id=%s LIMIT 5",
            (tenant_id, session_user_id))
```

- `session_user_id` 来自会话值的 `userId` 字段。
- 表内多为历史/演示数据；**没有订单就如实记录"当前会话无可归属订单"，不要编造**。

## 5. 公网验收（真实端到端）

统一头（`third-session` 全小写连字符，§2.1 of frontend-api-brief）：

```bash
curl -s -H "third-session: <会话>" -H "tenant-id: <会话租户>" \
  -H "X-Business-Entry: consumer" -H "Content-Type: application/json" \
  https://api.mall.qushiyun.com/v1/shortcuts
```

关键断言与对应契约见 `docs/agents/frontend-api-brief.md` 场景 D 与
`qa-plan.md` 的 INTENT-01..08；完整可复跑步骤见 `docs/validation.md`。

### 5.1 轮询纪律

- `qa`/`diagnosis` 创建返回 `202` + `retry_after_ms`，按它节流轮询。
- `failed` 是**终态**：停止轮询，`error` 带 `{code, message, retryable}`。
- 诊断是长任务（实测 1-8 分钟），用后台轮询，不要短超时。

### 5.2 故障排查入口

| 症状 | 查哪里 |
|---|---|
| 路由/回答异常 | 41 `gateway.db`：`assistant_questions`、`standard_diagnoses`、`agent_run_metrics`（`route_type` 区分 faq/qa/diagnosis/clarification/promo） |
| 模型/工具行为 | `/var/lib/aiops-41/gateway/runs/run-*/events.jsonl` + `/opt/aiops-41/codex-home/sessions/**/rollout-*.jsonl` |
| 检索为何 0 命中 | 36 RAGFlow 容器日志：`docker logs ragflow-kb-ragflow-cpu-1 --since 20m \| grep match_text`（**能直接看到模型实际发出的检索词**） |
| 服务无响应 | `journalctl -u aiops-gateway-41.service`、`systemctl status`、`/health` |

**经验**：`match_text` 日志是排查"检索质量"问题的最快路径——本轮即由此发现
模型把整段 `reason` 当检索词导致 0 命中。

## 5.5 存量快捷动作的语言迁移（#542，2026-10-07 实做）

**何时需要**：新增受支持语言后。改种子**不等于**改线上 —— `seed_bundled` 跳过已存在的行，
所以在新语言之前发布的快捷动作永远拿不到新语言文案（请求新语言时 `served_language` 如实报 `zh`、
文案回退简体）。`docs/validation.md` 有 #542 的完整实测记录。

**工具**：`tools/migrate_shortcut_i18n.py`（随仓库走，**不要在 41 上手写 SQL**）。

### 步骤

```bash
# ① 备份（sqlite 在线 backup，服务不用停；记下 sha256 与时间戳）
ssh aiops-41 'TS=$(date +%Y%m%d-%H%M%S); mkdir -p /var/lib/aiops-41/backups
  /opt/aiops-41/.venv/bin/python - "$TS" <<PY
import sqlite3,sys
s=sqlite3.connect("file:/var/lib/aiops-41/gateway/gateway.db?mode=ro",uri=True)
d=sqlite3.connect("/var/lib/aiops-41/backups/gateway.db."+sys.argv[1]); s.backup(d); d.close()
PY
  sha256sum /var/lib/aiops-41/backups/gateway.db.$TS; echo BACKUP_TS=$TS'

# ② 把工具送上机（**只送工具**，不动 /opt/aiops-41 的源码 —— 那是部署产物）
#    scp 不会创建中间目录，首次执行必须先建好，否则上传就在这一步停住。
ssh aiops-41 'mkdir -p /tmp/mig542/tools'
scp tools/migrate_shortcut_i18n.py aiops-41:/tmp/mig542/tools/
ssh aiops-41 'chmod -R a+rX /tmp/mig542'

# ③ 预演（只读；打印每行将补哪些语言，不写任何东西）
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env PYTHONPATH=/opt/aiops-41/src \
  /opt/aiops-41/.venv/bin/python /tmp/mig542/tools/migrate_shortcut_i18n.py \
  --db /var/lib/aiops-41/gateway/gateway.db'

# ④ 应用（**必须 runuser -u aiops41**：私有目录守卫会拒绝 root；工作目录须为 /opt/aiops-41）
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env PYTHONPATH=/opt/aiops-41/src \
  /opt/aiops-41/.venv/bin/python /tmp/mig542/tools/migrate_shortcut_i18n.py \
  --db /var/lib/aiops-41/gateway/gateway.db --apply'

# ⑤ 复核（读；应 0 次回退）
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env PYTHONPATH=/opt/aiops-41/src \
  /opt/aiops-41/.venv/bin/python - <<PY
from pathlib import Path
import sqlite3
from aiops_diagnostics.shortcut_lifecycle import ShortcutStore
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES as L
store = ShortcutStore(Path("/var/lib/aiops-41/gateway/gateway.db"))
# 请求返回的是【按租户合并】后的列表，所以要按**真实租户**逐个复核，不能只看平台行：
# 租户发布了同名覆盖动作时，其译文缺口不会出现在平台行的统计里。
# （平台行用 list_published —— list_effective 会拒绝保留的平台租户 ID。）
def check(label, rows):
    bad = [(r.code, lang, r.public(lang)["language"])
           for r in rows for lang in L if r.public(lang)["language"] != lang]
    print(f"{label} 回退次数:", len(bad), bad)

for entry in ("operator", "consumer"):
    check(f"__platform__/{entry}", store.list_published("__platform__", entry))

conn = sqlite3.connect("file:/var/lib/aiops-41/gateway/gateway.db?mode=ro", uri=True)
tenants = [r[0] for r in conn.execute("select distinct tenant_id from shortcuts where tenant_id != '__platform__'")]
conn.close()
for tenant in tenants:
    for entry in ("operator", "consumer"):
        check(f"{tenant}/{entry}", store.list_effective(tenant, entry))
PY'
```

### 恢复：优先**按动作回滚**，不要恢复整库

⚠️ **整库恢复会丢掉备份之后的所有写入** —— `gateway.db` 里还有诊断作业、会话、设备与租户数据，
不只是快捷动作。整库恢复只作为**灾难恢复**手段，且必须先对当前状态再备一份。
平时用**按动作回滚**：迁移为每个被改动作生成了**新的不可变版本**，
`ShortcutManager.rollback` 用任意旧 `version_no` 恢复其文案；`disabled` 行本工具跳过、未被改动。

**先查版本，再回滚。回滚自身也会生成一条新版本，所以它同样可逆。**

```bash
# ① 查该动作的版本历史（库无 sqlite3 CLI，用 python 只读查）。
#    迁移新生成的版本号最大；挑选它之前的那一版。labels 的语言数一眼可辨
#    （迁移前 6 语、迁移后 11 语）。
ssh aiops-41 'V=/opt/aiops-41/.venv/bin/python; $V - <<PY
import json, sqlite3
c = sqlite3.connect("file:/var/lib/aiops-41/gateway/gateway.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
sid = (c.execute("select shortcut_id from shortcuts where tenant_id=? and business_entry=? and code=?",
                 ("__platform__", "operator", "case_exploration")).fetchone())["shortcut_id"]
print("shortcut_id =", sid)
for v in c.execute("select version_no, published_at, snapshot_json from shortcut_versions where shortcut_id=? order by version_no", (sid,)):
    snap = json.loads(v["snapshot_json"])
    labels = snap.get("labels") or snap.get("fields_json") or {}
    if isinstance(labels, str): labels = json.loads(labels)
    print(" v%d  %s  labels=%d 语" % (v["version_no"], v["published_at"][:19], len(labels.get("labels") or labels)))
PY'

# ② 回滚到迁移前那一版（把 <version_no> 换成 ① 里挑出的编号）。
#    expected_revision 必须是**当前** revision —— 先读出来再回滚，两步之间不要有别的编辑。
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env PYTHONPATH=/opt/aiops-41/src \
  /opt/aiops-41/.venv/bin/python - <<PY
from pathlib import Path
from aiops_diagnostics.shortcut_lifecycle import PLATFORM_SCOPE, ShortcutManager, ShortcutStore
store = ShortcutStore(Path("/var/lib/aiops-41/gateway/gateway.db")); mgr = ShortcutManager(store)
ctx = type("Ctx", (), {"effective_tenant_id": "__platform__", "roles": frozenset({"ROLE_PLATFORM_ADMIN"}),
                       "caller": type("U", (), {"b_user_id": "rollback"})()})()
sid = "<上一步打印的 shortcut_id>"
live = mgr.get(ctx, sid, scope=PLATFORM_SCOPE)
print("当前 revision =", live.revision, " 当前 labels 语言数 =", len(live.labels))
mgr.rollback(ctx, sid, version_no=<迁移前的 version_no>, expected_revision=live.revision, scope=PLATFORM_SCOPE)
print("回滚后 =", len(mgr.get(ctx, sid, scope=PLATFORM_SCOPE).labels), "语（应为 6）")
PY'

# ③ 复核（与迁移的复核同一条命令）
```

⚠️ `rollback` **只接受 `published` 行**，且回滚会把行恢复成 `published` —— 本工具不动停用行，故无此问题。

### 本工具不做的

- **草稿不发布**（发布一个未批准的动作比缺翻译严重得多）；草稿就地补，仍是草稿。
- **停用行跳过并报告** —— 改它要 enable→edit→disable，中途崩溃会把该行**Enable 且用户可见**。
- **不覆盖已有值** —— 种子只补缺失语言；运营/租户自己的文案逐字保留。
- **无种子的动作（`solution_discovery`）报为缺口**，不臆造文案。

## 5.6 回答面的真用户端到端探测（2026-10-07 新增，含零写入默认）

**何时用**：要证明**用户真正读到的面**（`/v1/faq/*`、`/v1/shortcuts`）在生产上真能服务一个真实用户。
控制面（健康/注册/run）用 `verify-aiops-gateway`，那个跑在一次性数据根上、不碰生产。

**技能**：`.claude/skills/verify-aiops-client-e2e/`。下面是最短可行命令。

### ⚠️ 先读这一条：探测会写，且写的是别人的作业

`create_gateway_app` 内部调 `recover_interrupted_jobs()`，**把每一条 queued/running 作业标 failed**。
在启动路径上这是对的（持有那些作业的进程确实死了）；但探测是**在网关仍在运行时**再构造一个 app，
于是会把**正在被 worker 处理**的作业标失败。**只读请求不会撤销这次写入。**

脚本因此默认**不构造 app**（只打印计划、零写入）。真要探测时：

```bash
# ① 确认没有在飞作业。非空就别跑。
ssh aiops-41 '/opt/aiops-41/.venv/bin/python -c "
import sqlite3
c = sqlite3.connect(\"file:/var/lib/aiops-41/gateway/gateway.db?mode=ro\", uri=True)
for t in (\"standard_diagnoses\",\"assistant_questions\",\"health_report_jobs\"):
    print(t, dict(c.execute(f\"select status,count(*) from {t} where status in (\x27queued\x27,\x27running\x27) group by status\").fetchall()))"'

# ② 探测
scp .claude/skills/verify-aiops-client-e2e/scripts/probe_gateway_as_real_user.py aiops-41:/tmp/probe.py
ssh aiops-41 'chmod a+r /tmp/probe.py'
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env \
  $(tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
    | grep -E "^AIOPS_" | xargs -d"\n") \
  /opt/aiops-41/.venv/bin/python /tmp/probe.py --accept-live-app --languages zh,en,zh-Hant,vi,th,km'

# ③ 复核对账：探测后的状态应与 ① 的应答一致（除本次正常完成的请求外无新增 failed）
```

**为什么必须 `runuser -u aiops41` + 服务自己的 env**：配置是 `0600 aiops41`（root 也读不到），
且 `GatewayServerSettings.from_env()` 读的是**网关的** env，不是某个交互 shell 的。

**判据三合一**：HTTP 200 **且** `language` == 请求语言 **且** 有内容。
只报 200 会把「回显请求语言却服务兜底文案」判成通过；空列表会把「没有任何可看的按钮」判成通过。

**凭据**：脚本读服务令牌 / Redis 口令 / 一个真实会话，**一个都不打印**。会话本身是凭据，
用它即等同该用户身份直到过期 —— 不要把探测输出贴进任何会被提交的文件。

### 客户端流量：先问「调没调」

```bash
ssh aiops-41 'python3 /dev/stdin /www/wwwlogs/<vhost>.log --since <DD/Mon/YYYY> --ua Html5Plus --missing /v1/shortcuts' \
  < .claude/skills/verify-aiops-client-e2e/scripts/what_does_the_client_call.py
```

退出码 **0 = 有请求 / 1 = 确实 0 次 / 2 = 日志读不到**。
**三种不要混用**：把「读不到」当成「零请求」，会得到一个关于某个路由、而没人真的看过的自信错判。
`grep -l "v1/<route>" /www/wwwlogs/*.log` 反查 vhost，不要假设客户端用的是哪个域名。

## 6. 真实验收边界（不得逾越）

- 本地 fixture / 替身模型 / SQLite 直查 / 模型单次调用，**都不是**真实业务验收。
- 宣传卡片需该租户有**已发布宣传 Agent + 绑定 KB + 至少一条素材**；无素材时
  "无可用案例"是正确行为，不要宣称卡片验收通过。
- 涉及写操作（工单/退款/配置）保持禁用；本手册所有步骤为只读或经生产代码路径
  的资源创建。
- 报告状态只能用：`draft_ready` / `pr_open` / `merged_waiting_deploy` / `live` /
  `blocked`；未实测一律写"待验证"。
