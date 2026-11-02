# 站点说明拒收修复的合并部署与真实入口验收（2f38f4dc，2026-11-02）

合并：#1310 → `2f38f4dc1aaa3caca3a8191efd398250eaaf9e1c`。本文件记录**实际执行**的部署与
真实入口观测，不含预期。前序：[拒收定位](2026-11-02-site-answer-refusal-evidence.md)、
[根因与修复](2026-11-02-site-note-generic-noun.md)。

## 一、部署

CD `41449283605` 自动触发并成功（Deploy 与 Publish release notes 两步均 success）。

| 项 | 值 | 核验 |
|---|---|---|
| `/health` 版本 | `1.0.0+2f38f4dc1aaa` | 部署后 13:01 读取 |
| 服务状态 | `active` | `systemctl is-active aiops-gateway-41` |
| 部署源码 `operator_conversation.py` | `c36161e5…6afb` | 与 `git show 2f38f4dc:` 的 sha256 **逐字节相同** |
| 部署前版本 | `1.0.0+2e98be79de23` | 合并 #1309 后的版本 |
| 备份 | `/var/backups/aiops-41/backup-20261102-210112` | 部署脚本自动生成 |

部署前预检：在飞 agent_run 0、在飞问答 0；近 30 分钟只有消费端两次发布（11:55 v15、
12:22 v16）与一次管家版本恢复（12:04），无并发部署。

## 二、真实入口验收（原管家 H5 → 公网入口 → 网关 → 节点 → CPMS）

用加密清单账号 `aiops-ulink-account-20261030` 在原管家 H5 **正常登录**（首页显示
「你好，ulink」），随后在聊天页提问。全程未注入凭据、未自签身份。

### 用例一：列举（此前必失败的形状）

问「我有哪些站点？」，HTTP `POST /v1/assistant/questions` → *202*，`qa_7d1c3875…`，
状态推进到 **completed**（`error: null`）。

答复正文（节选，逐字）：

```text
我帮你查了一下，当前账号可见的地点已列在下方。列表可能较长，需要的话可以用站名或地点关键词缩小范围，我再帮你筛选。
当前账号可见范围内共 160 个站点，这里显示前 10 个：
1. ZDH--Test2026-11-02 03:00:08 — 984HF52X+QW
4. بغداد مول (Baghdad Mall) — Baghdad, Baghdad Governorate, Iraq
…
未提供定位，未按距离排序；本次结果不表示实时空闲枪口或价格。
查询达到显示或分页上限，结果可能不完整，请缩小地点范围。
cpms.sitePage · 2026-11-02T13:11:05Z
另有 150 个站点未在此列出。
```

服务端逐项核对（只读库）：

| 项 | 值 |
|---|---|
| `assistant_questions.status` | `completed`，`error_code` 空 |
| `agent_runs.state` / `reason` | `succeeded` / 空 |
| `operation` / `route_revision` | `operator_conversation` / 16 |
| 证据工具 | `cpms_site_search`（非空，来源 `cpms.sitePage`） |
| 工具返回 | `total=160`、`samples_returned=50`、`truncated=true` |
| 渲染层 | 展示 10 条、`limited=true`、`empty_scope=false` |

关键点：模型写的开场正是此前被判「编造站名」的句式（「用站名或地点关键词缩小范围」），
这次**未被拒**；而事实（数量、名单、截断、来源）仍全部由系统写出。

### 用例二：关键词搜索

问「帮我找一下 ira 附近的充电站」，同样 **completed**。工具入参 `address=ira`、
返回 `total=7`、`returned=6`、`truncated=true`；答复列出 6 条并标注分页上限。

### 独立 CPMS 对照（列举）

用同一调用者的真实令牌直连公司接口（不经过本网关）：

```text
code=0, total=160, rows=50
first3 = ["ZDH--Test2026-11-02 03:00:08", "ZDH--Test2026-11-01 03:00:10", "ZDH--Test2026-10-31 03:00:09"]
tenants = ["1942105476598861824"]   # 与调用者租户一致
```

总数、首三条站名与答复逐字一致；所有行的 `tenantId` 都是调用者租户。

### 独立 CPMS 对照（关键词，含一处已解释的差异）

同一令牌直连 `chSite/page?address=ira`：

```text
服务端 total=7，7 条：
  بغداد مول (Baghdad Mall) / iraq-site 1 / Estação AC | Rancho do Cupim /
  Eletroposto Petronorte / Condominium Wings / Frames / Maui
按本地判据（name+address 的**字面子串**、casefold）再过滤后剩 6 条：
  丢弃 Eletroposto Petronorte
```

丢弃的原因查清了，而且不是"上游去重音、我们不去重音"这么对称：

- 本地判据是字面子串：`'ira' in 'ribeirão preto'.casefold()` → **False**；
- 对同一串做 `unicodedata.normalize('NFD')` 去掉 `Mn` 类字符后 → **True**。

也就是说**重音字母把关键字劈开了** —— `ribeir-ã-o` 里的 `ira` 被 `ã` 隔断。服务端能命中
（它按折叠后的文本比较），我们的字面判据不能。所以两边差 1 条。

答复展示的 6 条**恰好就是这个交集**，与证据里的 `total=7 / returned=6 / truncated=true`
完全一致；答复也未声称"共 N 个匹配"（搜索问法不给总数，只提示分页上限）。

这是**本地判据更严**的方向：只会少列服务端返回的行，不会多列。它不属于本次改动引入的
问题，本片不改它；记录在此以免下一位读者把"7 与 6 不一致"当成缺陷。

判据方向是单向的，所以只需防一个方向：**若哪天我们列得比上游多，那才是真缺陷**。

### 服务端日志

部署后两次真实请求期间，`aiops-gateway-41` 日志中**没有** `operator_answer_rejected` 或
`operator_completion_refused` 记录；两条问答都以 `completed` 结束。

## 三、下一步

按条件排先后，缺口需要不同的真实账号：

1. **受限范围与跨租户拒绝**（需要受限账号）：现有验收账号是租户顶层，看不到这两条面。
   先确认加密清单里是否有受限运营账号；没有就向公司侧申请一个，再验「越权站点不出现在
   结果里」与「受限账号只看得到分配站点」。
2. **合法空结果**（现有账号可做）：关键词搜一个必然无命中且无歧义的串，确认答复是
   「未找到匹配站点」而不是「你没有站点」；再用无关站点集账号验列举空。
3. **D（两端原入口生命周期）**：管家端本轮已通过，消费者端要等 slots 契约那边收口。
4. **H1 / I**：依赖受限账号与消费者端，排在上面之后。

不在这份记录里升级任何判定：D、H1、I 仍按未验收处理。

## 四、判定

- 站点**列举**与**关键词搜索**两条真实正例通过，且失败面是此前复现多次的那一个。
- 需在 41 内网执行才能对照 CPMS；开发机直连公网超时，这一点沿用既有记录。

**仍然未验收**（不因本片升级）：

- 合法空结果（本站点集为空 / 搜索无命中）在真实账号上的表现；
- 受限范围与跨租户拒绝需要真实受限账号，当前验收账号是租户顶层；
- D（两端原入口生命周期）、H1（消费者字段对齐）、I（受限入口）；
- 消费者链路的 `CHATFLOW_ANSWER_INVALID` 属另一条判据，不在本片范围。
