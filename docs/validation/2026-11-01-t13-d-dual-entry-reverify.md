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
chat_route_bindings（tenant 1942…，取得前后各读一次，完全相同）
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

**业务回答这一半两端都没过**，且原因不同：

### 2.1 管家端：运行清单里没有工具

3 次重复（另加 1 次页面操作，共 4 次）**全部**失败，日志逐字：

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
所以不是声明缺失。**本记录只证明现象与定位路径，不作根因断言**——需要在 36 的 Dify 控制台
看该 app 实际勾选。

**回归证据**：2026-10-31 用同一个问题（「列出本人可见场站及范围」）在同一入口成功过
（[`2026-10-31-z-onboarding-resume.md`](2026-10-31-z-onboarding-resume.md)：`202 / completed`，
正式路由 rev 5）。当前 rev 12 下 4/4 失败。

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

按语言（含本轮，GROUP BY 全表）：
  en       33 条：failed  4（12.1%）
  zh      113 条：failed 13（11.5%）
  其余七种语言合计 24 条：failed 0
```

**按语言没有差异**：`en` 与 `zh` 的失败率同一量级，本轮这次 `en` 失败不是语言造成的。
初版写成"en 29 次全成功、13 次失败全是 zh"，是把 `tail` 截断后的半张表当成了全表——
`en` 另有 4 条失败被截掉。教训与本仓库已记过的一条同型：**计数一律用 `GROUP BY` 出全表，
不要用管道尾部当结论**。

这 17 条的构成也不是单一原因：

| 原因 | 条数 |
| --- | --- |
| `CHATFLOW_ANSWER_INVALID` | 12（zh 11 / en 1） |
| `trial_failed` | 2（zh） |
| `DIFY_CHATFLOW_INVALID` / `CHATFLOW_EVIDENCE_UNAVAILABLE` / `CHATFLOW_UNAVAILABLE` | 各 1（en） |

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

因此 D 判 **部分（路由与登录已验，双端回答未通过）**，与上一轮的「部分」相比，
**未通过的原因已经从"没做"变成"做了、并查出两个具体问题"**。

## 四、方法上值得记的两点

1. **4 位验证码配 6 位输入框**：消费者端页面提示 "Enter the 6-digit OTP"，而测试环境实际
   发的是 4 位码。按页面提示判断"码不对"会走偏；以 Redis 里当次值为准。
2. **`.mode-btn` 才能切到键盘输入**：两端聊天页默认都是语音（`按住说话`），
   不先点这个按钮，输入框根本不存在——上一轮 H5 验收也踩过同一条。

## 五、清理

浏览器、SOCKS 隧道（`127.0.0.1:10801`）已关闭；本机会话态与验证码临时文件已删除。
原始响应、截图、订单号留在私有证据目录，未上传。
