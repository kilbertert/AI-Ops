---
name: verify-aiops-client-e2e
description: "在网关主机上以真实用户身份驱动**生产** AI-Ops 网关：解析真实 thirdSession、调用回答面（/v1/faq/*、/v1/shortcuts），并向访问日志追问部署出去的客户端到底调了什么。用于必须端到端证明用户可见行为时，或客户端侧症状需要定位成因（从没调用 / 调了但失败 / 版本不对）时。"
---

# 客户端回答面的端到端联调

本技能回答两个仓库里没有别的东西能回答的问题：

1. **部署出去的网关此刻真的能服务一个真实用户吗？** 回答面所有路由都拒绝设备令牌（401），
   所以一次性数据根那个技能**根本够不到它们**。本技能解析一个**真实会话**，
   用**生产配置**构造应用并驱动其真实 ASGI 面。
2. **部署出去的客户端到底调了什么？** 生产访问日志能把「客户端根本没调」与「调了但失败」
   分开 —— 这两种在用户那里长得一模一样，却是相反的问题。

验**控制面**（健康、注册、run 路由）请用 `verify-aiops-gateway`：它跑在一次性数据根上，
不碰任何真实环境，那才是对的工具。

## 边界：什么在哪里跑

| 部分 | 跑在哪 | 碰什么 |
|---|---|---|
| `probe_gateway_as_real_user.py` | **网关主机（41）** | 生产配置 + 一个真实会话；**默认零写入**（见下） |
| `what_does_the_client_call.py` | **持有访问日志的主机** | 只读日志文件 |

两者都不是 `TestClient` 冒烟测试：前者用**生产配置**构造**生产应用**并驱动它；
后者读的是**真实流量**。两者都逐语言打印证据，失败时退出码非零。

## ⚠️ 跑生产探测前必读：它会写，且写的是别人的作业

`create_gateway_app` 内部会调 `GatewayStore(...).recover_interrupted_jobs()`，
**把每一条 queued/running 的作业标成 failed**。这在网关**启动路径**上是对的
（持有那些作业的进程确实死了），但探测是**在网关仍在运行时**再构造一个 app ——
于是会把**正在被真实 worker 处理**的作业标失败，而用户的轮询随后读到 `failed`。
**只读的请求不会撤销这次写入。**

因此脚本的默认行为是**不构造 app**，只打印「将要探测什么」，退出 0：

```bash
# 默认：零写入，只列计划
python /tmp/probe.py --languages zh,en
#   ℹ️  未构造 app（那会调用 recover_interrupted_jobs，把在飞作业标失败）
#   (计划) zh GET /v1/faq/recommendations

# 真的要探测：先确认没有在飞作业，再显式承担
python /tmp/probe.py --accept-live-app --languages zh,en,zh-Hant,vi,mn,th,km
```

`--accept-live-app` 的用法与判据：

```bash
# 探测前：应为空。非空就别跑，或者等它结束。
ssh aiops-41 '/opt/aiops-41/.venv/bin/python -c "
import sqlite3
c = sqlite3.connect(\"file:/var/lib/aiops-41/gateway/gateway.db?mode=ro\", uri=True)
for t in (\"standard_diagnoses\",\"assistant_questions\",\"health_report_jobs\"):
    print(t, dict(c.execute(f\"select status,count(*) from {t} where status in (\x27queued\x27,\x27running\x27) group by status\").fetchall()))"'
```

探测后**再查一次**并把两次结果一起写进证据。

### 判据是「200 **且** served == 请求语言 **且** 有内容」

三个条件缺一不可：

- **不是「请求成功」**：200 回显 `zh-Hant` 却带简体文案，正是这套工作流反复要消灭的形态；
- **不是「有响应」**：某门语言缺文案时，路由仍会回显请求语言，只有 `language` 字段会如实报实际服务的语言；
- **空列表不算通过**：一个承诺有内容的面返回 0 条，说明**没有可看的用户界面**，不是通过。
  只为探可达性时才加 `--allow-empty`，并在记录里写明那是可达性而非内容验证。

`--show-copy` 默认关闭：它打印的是**生产内容**，会进入 agent 的 transcript。判定靠计数与
`served` 语言，标题对判定没有增量。

## 跑生产探测（完整命令）

```bash
# ① 送脚本上机并让服务账号可读
scp .claude/skills/verify-aiops-client-e2e/scripts/probe_gateway_as_real_user.py aiops-41:/tmp/probe.py
ssh aiops-41 'chmod a+r /tmp/probe.py'

# ② 用**服务自己的环境**、以**服务账号**运行。两者都不能省：
#    - GatewayServerSettings.from_env() 读的是网关的 env，不是某个 shell 的；
#    - 配置是 0600 aiops41，root 也读不到。
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env \
  $(tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
    | grep -E "^AIOPS_" | xargs -d"\n") \
  /opt/aiops-41/.venv/bin/python /tmp/probe.py --accept-live-app --languages zh,en,zh-Hant,vi,th,km'
```

换成管家端内容域：`--entry operator --paths /v1/faq/catalog`。

### 两个最费时间的坑

- **头名是 `X-Third-Session`。** FastAPI 会剥掉头名里的下划线，小写 `third-session`
  到不了处理函数、变成 `None`，而解析器报的是 **"service authentication failed"** ——
  **把矛头指向令牌**。令牌明明是对的却报这个，先查头名拼写。
- **会话值是 Redis 的键，不是它的值。** 键是 `app:3rd_session:<x>`，请求头带 `<x>`；
  对该键 `GET` 拿到的是 Java 序列化 blob，由解析器自己解。

### 凭据纪律

脚本读三样东西：服务令牌、Redis 口令、以及一个**真实用户会话**（会话本身就是凭据，
用它即等同该用户身份，直到过期）。**三者一个都不打印、都不落文件。** 需要引用一次运行结果时，
引用计数与服务的语言，不要引用素材本身。

## 跑客户端流量探测

```bash
ssh aiops-41 'python3 /dev/stdin /www/wwwlogs/api.mall.qushiyun.com.log \
  --since 07/Oct/2026 --ua Html5Plus --missing /v1/shortcuts' \
  < .claude/skills/verify-aiops-client-e2e/scripts/what_does_the_client_call.py
```

退出码分三种，**不要混用**：

| 码 | 含义 |
|---|---|
| 0 | 该路径**有**请求 |
| 1 | 日志读到了，窗口内该路径**确实 0 次** |
| 2 | **日志读不到** —— 这既不是「有」也不是「没有」，是没看成 |

探测脚本用同一套约定：**0 = 全部通过、1 = 有失败、2 = 未取证**
（默认的"只列计划"模式返回 2，因为**一个请求都没发，什么都没被验证** ——
把计划读成结论，正是本技能要防的那种假陈述）。

**零请求与任何错误码都是不同的结论**：请求失败会留下 4xx/5xx 行，
**一行都没有**说明请求根本没发出去。把「读不到」当成「没有」会得到一个关于某个路由的、
没人真的看过的自信错判 —— 所以脚本把这两件事分开报。

查哪个日志文件：AI-Ops 路由是按 vhost 反代的，客户端自己的域名未必是你以为的那个。
用 `grep -l "v1/<route>" /www/wwwlogs/*.log` 反查 vhost，不要假设。

## 客户端侧决策树

功能在用户那里看不见时，先判定是下面哪一类，**再**去推理。每一步都很便宜，且各自排掉一整支：

1. **后端是否服务它？** → 跑生产探测。通过 ⇒ 后端不是成因，继续读后端源码不会找到答案。
2. **客户端是否请求它？** → 跑流量探测。**零请求就终结了后端这一支**。
3. **部署出去的客户端有能力请求吗？** → 反编译客户端包。uni-app 的 APK 业务 JS 在
   `assets/apps/__UNI__*/www/app-service.js`；对 15MB 的包用 Python 正则比 `ssh | grep` 快
   （后者会超时）。这一步把「这个版本从来没有这个调用」与「调用在，只是版本早于该功能」分开。
4. **用户在哪个版本上？** → User-Agent 里带 uni-app 运行时版本（`uni-app (Immersed/<n>)`）。
   拿它与手上的构版本比对，因为「功能被删了」与「用户在一个早于该功能的构建上」有着同一张截图。

### 读客户端包时不要猜

`assets/apps/__UNI__*/www/manifest.json` 里有 `version.name` / `version.code` ——
这是唯一能可靠说明「这个包是哪个构建」的做法；PGYer 页面标题可能显示的是**最新**构建，
而不是它实际给你的那个文件。

## 说清楚证不了什么

- **模型侧回答**：`/v1/assistant/questions` 与一次真实诊断需要 provider key 与实时数据源。
  探测能说明路由可达，**不能**说明模型照着语言写了。
- **前端渲染出来了**：200 且文案正确只证明后端；不证明用户看见了。
  忽略响应的客户端位于本技能所有检查之外 —— 见上面第 3/4 步与前端交接文档。
- **译文质量**：没有任何自动化检查能判定一段译文读起来是否通顺。

## 关联文档

- `verify-aiops-gateway` —— 控制面，一次性数据根，不接触生产。
- `docs/agents/env-41-runbook.md` —— 主机事实、两个 env 文件的分工、部署方式。
- `docs/agents/frontend-operator-handoff.md` —— 管家端前端契约。
- `docs/reviews/2026-10-07-i18n-program-retrospective.md` —— 语言轴的整体完成度与已知缺口。
