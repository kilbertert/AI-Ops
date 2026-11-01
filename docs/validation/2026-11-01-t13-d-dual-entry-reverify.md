# T13 第 D 项补做：原入口双端复验（2026-11-01）

执行时间：2026-11-01（UTC 14:20–14:45 / 北京时间 22:20–22:45）。环境：41 生产网关。
目的：把 A–L 矩阵里 D 行的「**切换前后的原入口双端复验**」补齐——用真实账号在原 H5 与
原管家 H5 上各跑一次，比对两端是否都落在**当前正式路由**上。

方法遵守
[正常登录与原页面验收](../../agents/runtime-retirement-operations.md#正常登录与原页面验收)：
开工前查活跃发布、失败先证明租户/入口/场景、取证前后各读一次绑定 `revision`。

## 一、开工前状态（前后一致，取证期间无人改生产）

```
publish_audit_ledger 近 30 分钟：0 条
chat_route_bindings（tenant 1942…，取证前后各读一次，完全相同）
  consumer / consumer_own_order_status  active  rev 1   v18
  consumer / consumer_service           active  rev 14  v13
  operator / operator_order_diagnosis   active  rev 12  v2
```

## 二、双端复验结果

| | 管家端（员工） | 消费者端 |
| --- | --- | --- |
| 原入口 | `ulinkmanage.h5.mall.qushiyun.com` | `ulink.h5.mall.qushiyun.com` |
| 真实登录 | 账号口令 → `/upms/token/login` 200，`CLOUD_ACCESS_TOKEN` 写入 | 手机号发码（`/mallapi/phone/code` 200）→ `/mallapi/userinfo/phone/login` 成功，`third_session` 写入 |
| 页面问答 | 「智能检测」→ 助手页 → 发送 | `Ai Trendi` 聊天页 → 发送 |
| 请求 | `POST /v1/assistant/questions` 202 → 轮询 200 | 同左 |
| **路由归属** | `operator_order_diagnosis:production-41:1942…`，**rev 12**，`exec_config_state=matched` | `consumer_service:production-41:1942…`，**rev 14**，`agent_version_no=13` |
| **本轮结果** | **failed** `OPERATOR_DIAGNOSIS_FAILED` | **failed** `CHATFLOW_ANSWER_INVALID` |
| 页面表现 | 「本次回答没能完成生成，请稍后重试」 | "This answer could not be generated" |

**路由这一半是通过的**：两端请求都落在**当前正式绑定**上（rev 12 / rev 14），
不是旧应用、不是未登记卡片、也没有回落到原客服应用（`394b5399`）。
原应用与新应用的切换没有把任何一端的入口打断。

**业务回答这一半两端都没过**，且原因不同。

### 2.1 管家端：运行清单里没有工具

4 次尝试（1 次页面操作 + 3 次同问题重复）**全部**失败，日志逐字：

```
WARNING aiops.operator operator_completion_refused code=OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE
```

`agent_runs`：

```
state=failed  reason=operator_failed  route_revision=12  actual_node_id=NULL
```

根因可直接读出来——该 run 的 `execution_manifest.tools` 是 **`[]`**。而同一份 manifest 的
system message 明确要求：

> 找站、查站点位置时**先调用 cpms_site_search** 取得当前账号可见的真实结果，再据此写一两句说明。

工具表为空 ⇒ 节点无从调用 ⇒ 只能写一段没有取数依据的说明 ⇒ 网关按
`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收（`src/aiops_diagnostics/operator/operator_cpms.py`
里这条判据的注释写得很清楚：**不能把"看到结果之前"写下的话当查询结果交付**）。

工具表为空的两个可能来源，都在 `node_publication.candidate()` 的过滤链上：

1. 模板 `target.manifest_file` 的 `raw["tools"]` 为空；
2. Dify 侧 `agent_mode.tools` 没勾 `cpms_site_search`（`allowed` 为空 ⇒ `body["tools"]=[]`）。

`cpms_site_search` 的声明本身是对的（`operator_tools.json`：`applies_to: ["conversation"]`），
所以不是声明缺失。

### 根因（2026-11-01 追加定位，经两轮订正）

**先说结论：不是"节点没实现站点工具"，也不是"Dify 侧没勾"。** 这条路径上，
**对话回合压根不会下发任何工具**。

#### 走的是哪份清单

```
/etc/aiops-41/gateway.env
  AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE=/etc/aiops-node-runtime/gateway/node-targets.json
operator 目标的 manifest_file = operator-exec-manifest.json（订单诊断用）
但本次路由的是 operator 的 conversation 分支
  → operator_cpms.py:375  manifest_text = operator_conversation.manifest(binding.manifest_text)
```

`operator_conversation.manifest()`（`operator_conversation.py:87`）在末尾**无条件把工具表清空**：

```python
body["tools"] = kept          # 只留 applies_to 含 "conversation" 的
...
# 而 INSTRUCTIONS 里写着：本轮没有选定订单，不提供订单工具
```

模板里只有 `cpms_order_detail` / `cpms_charging_samples`，两者 `applies_to` 都不含
`conversation`，于是 `kept=[]`。**所以 `tools=[]` 是这条分支的设计结果，不是配置缺项。**

#### 真正的矛盾在提示词里

同一次运行下发的 system message 自相矛盾：

| 句子 | 出处 |
| --- | --- |
| 「找站、查站点位置时**先调用 `cpms_site_search`** 取得当前账号可见的真实结果」 | `operator_conversation.INSTRUCTIONS` |
| 「系统按当前账号可见范围列出站点；你不要补关键词，也**不要在 text 里写名单**」 | 同上 |
| 「没有调用工具就没有结果，此时 **text 必须为空**」 | 同上 |
| 「**没有检索工具**，不得声称读过知识库或业务数据」（前半句在追加业务背景**之后**） | 同上 |

最后一句里的「检索工具」指的是知识库检索，不是站点工具——但它和前三句放在同一段里，
并且是**唯一一句说"没有工具"的话**。模型据此写了说明文字，网关按
`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收。

#### 订正两处此前的说法

1. 上一版说「节点模板从来没声明 `cpms_site_search`，是 P3 未做」——**这句不成立**。
   节点侧已按声明装配：`GovernedToolRuntime.resolvedTools()` 对
   `OperatorTools.declares(name)` 为真的工具会构造 `CpmsReadTool`，而
   `OperatorTools` 读的正是 `operator_tools.json`（其中 `cpms_site_search` 有声明）。
   缺的是**对话分支不下发工具**，不是节点不会执行。
   （另：`tool-architecture.md` §6 那张"未实现"的表已过期，与本条不符，不该拿来当依据。）
2. 上一版说这是「回归」——**也不准确**。2026-10-31 成功那次，模型回的是
   `kind=site_search` + **空 text**，网关走的是自己调 `query_sites()` 的分支
   （`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 的反面），所以能成。
   现在提示词让模型"再据此写一两句说明"，一写字就撞上拒收。**是提示词与
   "对话分支不下发工具"这条设计互相打架**，不是同一条代码路径的回归。

#### 仍待确认的一点

`operator-exec-manifest.json` 的 mtime 是 2026-10-26，而对话分支本来就不用它，
所以它是否该含 `cpms_site_search` **与本次失败无关**——但它和
`tool-architecture.md` 的记录不一致，属于另一件事，本文不展开。

### 2.2 消费者端：偶发解析不出合法 JSON

单次失败：

```
chat_dify_runs  state=failed  reason=CHATFLOW_ANSWER_INVALID
reject_detail   invalid document answer (json_syntax)
data_kind=status   language=en
```

落点是 `consumer_documents.answer_selection` 的 `json.loads`。但**不是必现**：

```
同租户 chat_dify_runs 总计 170 条，state != succeeded 17 条（10.0%）
2026-11-01 14:37 两次同路径请求 succeeded（language=zh）
```

**先按原因拆，再按语言比。** 17 条失败不是同一个原因，混在一起比没有意义：

| 原因 | 条数 |
| --- | --- |
| `CHATFLOW_ANSWER_INVALID` | 12（zh 11 / en 1） |
| `trial_failed` | 2（zh） |
| `DIFY_CHATFLOW_INVALID` / `CHATFLOW_EVIDENCE_UNAVAILABLE` / `CHATFLOW_UNAVAILABLE` | 各 1（en） |

只统计**同一原因**（`CHATFLOW_ANSWER_INVALID`）的分布：

```
en  n=33   invalid= 1  → 3.0%
zh  n=113  invalid=11  → 9.7%
其余七种语言合计 n=24  invalid=0
```

也就是说：把别的失败原因剔除后，`zh` 的解析失败率约为 `en` 的三倍。

**但这不足以证明语言是原因**——`en` 的分子只有 1 条，样本太薄，两者差异落在随机波动内
完全可能；上文那张混算表（`en` 4/33 vs `zh` 13/113）更不能用来下结论，因为它把三种不同
的失败原因掺在了一起。所以本记录的立场是：**"与语言无关"这个说法不成立，我撤回；
"语言是原因"同样未被证明**。要判定需要按语言分层、且同原因的更大样本。
本轮这次 `en` 失败只作为一个观测点，不作为推断依据。

`json_syntax` 这个标记的语义要说准：它表示**输出以 `{` 或 `[` 开头但解析失败**，
不等于"模型没吐 JSON"。截断、转义错误、尾逗号都落在这一档，标记本身分不出来。
所以本文只说"这一轮没解析出合法 JSON"，不说"模型回答了纯文本"。

此项已在另处处理中：PR #1291（已合并 `833e15b7`）在 `json.loads` 失败处补了**形态**标记
（`plain_text:<n>` / `fenced` / `json_syntax` / `empty` / `not_text:<type>`），不记原文。
本记录与那条不重复：**这里只证明"原入口会发生"并给出频度与分布**，修法不在本 PR 内。

## 三、D 项结论

| 判据（`dual-entry-onboarding-plan.md` §8 D） | 结论 |
| --- | --- |
| 原版本/凭据引用/**路由不被重建** | **通过**：两端都落在当前正式绑定（rev 12 / rev 14），原应用与其历史版本仍在，取证前后 revision 未变 |
| 正常账号有证据 | **通过**：两端都用库外受控账号走真实页面登录，取得真实会话 |
| 异常有证据 | **通过**：两端的失败都定位到具体判据与数据行，不是"页面白了" |
| **双端真实可用** | **不通过**：两端助手本轮都失败 |

因此 D 判 **部分（路由与登录已验，双端回答未通过）**，与上一轮相比，
**未通过的原因已经从"没做"变成"做了、并查出两个具体问题"**。

## 四、方法上值得记的三点

1. **4 位验证码配 6 位输入框**：消费者端页面提示 "Enter the 6-digit OTP"，而测试环境实际
   发的是 4 位码。按页面提示判断"码不对"会走偏；以 Redis 里当次值为准。
2. **`.mode-btn` 才能切到键盘输入**：两端聊天页默认都是语音（`按住说话`），
   不先点这个按钮，输入框根本不存在——上一轮 H5 验收也踩过同一条。
3. **计数只用 `GROUP BY` 全表，不用管道尾部**：初版本节写成「`en` 29 次全成功、13 次失败
   全是 `zh`」，据此暗示语言相关。实际是 `| tail -10` 把 `en failed` 那一行截掉了，
   全表是 `en` 4/33、`zh` 13/113。但**这个混算本身也不能用**：它把三种失败原因掺在一起，
   按同一原因拆开后是 `zh` 11/113、`en` 1/33。本仓库已因同型问题记过一次
   （管道退出码 / 管道截断），这次是同一族。

## 五、清理

浏览器、SOCKS 隧道（`127.0.0.1:10801`）已关闭；本机会话态与验证码临时文件已删除。
原始响应、截图、订单号留在私有证据目录，未上传。
