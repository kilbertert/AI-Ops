# 助手入口等待态与取消：BFF / 前端交接契约

> **读者**：BFF/Java 组 + 客服前端组。本文是「输入框锁定与复原」「用户停止」契约的唯一完整描述。
> **状态**：后端已实现在本分支（PRD #346：`cancelled` 终态 #354 / 取消端点 #357 / 重启收敛 #356）。
> **未完成业务验收**：§5 的响应样例由回归测试在真实网关栈上抓取，**尚未在 41 公网链路跑过取消**——
> 联调前请按 §8 复跑一次。
> **一句话**：`qa` / `diagnosis` 的 `202` 进入等待态并锁输入框；`faq` / `clarification` 的同步 `200`
> **不锁**；**解锁只看作业终态，前端不要自己计时**；用户可按停止，作业转到 `cancelled` 终态，
> 界面显示「已停止」且**没有任何内容**。

---

## 0. BFF 必须先做的两件事（漏任一条，这份契约静默失效）

前端**不直连** AI-Ops 网关，链路是 前端 → Java BFF → AI-Ops。`frontend-api-brief.md` 里「建议
BFF 保留 `/v1/*` 原样透传」只是**建议，不是保证**，所以以下两条必须由 BFF 侧确认落地：

1. **放行新路径** `POST /v1/assistant/questions/{qa_id}/cancel`。没放行的表现是稳定的 `404`／
   `405`，用户点了停止没反应，而取消作业其实已经成功——前端无从区分。
2. **原样透传新状态值 `cancelled`**。取消在状态白名单里新增了一个值，BFF 若有响应词表白名单
   （enum 校验、DTO 字段映射），**不得静默改写或丢弃它**。被丢弃的表现是：轮询响应的 `status`
   变成别的值或整个字段消失，输入框要么永远不解锁、要么被判成失败。AI-Ops 的错误码与状态值
   一律英文下划线枚举，透传时不要做大小写或别名转换。

---

## 1. 谁进入等待态（谁锁输入框）

| 入口响应 | HTTP | 等待态 | 前端动作 |
|---|---:|---|---|
| `type=qa`（自由提问 / 宣传卡片） | 202 | **是** | 整个输入区替换为等待态；按 `retry_after_ms` 轮询 `GET /v1/assistant/questions/{qa_id}`；**可取消** |
| `type=diagnosis`（订单诊断） | 202 | **是** | 同上；轮询 `GET /v1/standard/diagnoses/{diagnosis_id}`；**当前不可取消**，见 §1.1 |
| `type=faq`（固定问答命中） | 200 | **否** | 直接渲染 `answer`，不锁、不轮询 |
| `type=clarification`（缺关键信息） | 200 | **否** | 渲染 `message`，按 `missing_fields` 补信息后重新提交 |
| 跳转类快捷动作 | **不请求** | **否** | 本地导航（`jump_path` 非 `null`），根本不进统一入口 |

- **判别只看 `type` 与 HTTP 状态码**：同步 `200` 永远不锁，不需要额外的字段。
- **锁粒度**：整个输入区（不只是发送按钮）替换为等待态；正在输入的文字**原样保留**。
- **停止后草稿恢复**：取消不碰输入框内容。用户放弃的是这次生成，不是他已经打好的字。
- 跳转类动作不经助手入口，因此既没有响应 `type` 也没有作业——**不要给它套等待态**，
  详见 [跳转动作交接](./frontend-jump-path-handoff.md)。

### 1.1 警告：诊断线会锁输入框，但**没有取消入口**（不要照抄 QA 的停止按钮）

`qa` 与 `diagnosis` **都进入等待态**，但**只有 `qa` 能取消**。诊断作业（`POST /v1/standard/diagnoses`
/ `GET /v1/standard/diagnoses/{diagnosis_id}`）**没有取消路由**——该路径上只有 `POST` 与两个 `GET`。
给诊断画一个调用 `/cancel` 的停止按钮，得到的会是稳定 `404`／`405`：**按钮永远不生效**，而用户会
以为是他没点到。

这个不对称是**刻意保留的**（PRD #346 的 Out of Scope：不为诊断开独立取消端点），不是漏做。所以：

- **诊断的等待态必须换一套文案与控件**：显示不可中断的等待（如「正在诊断，请稍候」），**不要**
  渲染停止按钮，也不要在本地超时后假装已停止。
- **诊断的等待时间比 QA 长得多**，用户更容易等不下去。若前端必须给一个出路，只能用「离开 /
  发起新对话」这类**不承诺中断后端作业**的动作，并明确它不会停止诊断。
- **等诊断自己到终态**：`completed` / `inconclusive` / `failed` / `expired` 四者都会解锁输入框，
  与 QA 一致（§2）。最坏情况是 15 分钟 deadline 清扫（`expired`）。
- 不要为了实现停止按钮而**自行**给诊断调 QA 的 `/cancel`：两者是不同资源、不同 id 空间，
  诊断 id 传给 `qa_id` 路径只会得到 404。

诊断获得取消能力属于后续范围；在那之前，本节就是它的契约。

---

## 2. 解锁由作业终态驱动（唯一权威）

- **解锁条件就是轮询响应里的 `status` 变成终态。** `retry_after_ms` 是它的伴生字段：
  非终态固定 `1000`，**终态为 `null`**。两者同时到达，所以「`retry_after_ms` 为 `null`」
  与「`status` 是终态」是同一个信号。
- 提问作业终态集合：`completed` / `failed` / `expired` / **`cancelled`**（诊断线另有
  `inconclusive`）。**四种终态都解锁，无一例外**——包括失败和过期。
- **前端不要另设超时兜底。** 后端只有两处兜底，都由它自己执行：作业 deadline 15 分钟
  （`queued`/`running` 一视同仁），会话生成槽 120 秒自过期。前端再叠一个计时器只会和这两条
  对不上，制造「按钮复原了但作业还在跑」或反之。
- **会话面的 `is_generating` 不是解锁条件**，它是这个会话**当前是否在生成**的并发闸门，
  供会话列表 / 详情页展示与刷新恢复使用。任何作业终态转移都会**协同释放**它（后端先写作业终态、
  再清会话槽位），所以终端用户看到的两件事同时发生；但权威只有一个：**作业的 `status`**。
  跨设备 / 刷新场景下 `is_generating` 最多还挂着 120 秒崩溃兜底，比作业终态晚，别用它解锁。
- 网关重启：重启前还 `queued`/`running` 的提问会被收敛为 `failed`（不是 `expired`、不是
  `cancelled`），`error.code=QA_INTERRUPTED_BY_RESTART`。**这不是用户停止，界面不要显示
  「已停止」**——它也是终态，同样解锁输入框。

---

## 3. 取消端点契约

```http
POST /v1/assistant/questions/{qa_id}/cancel
```

- **无请求体。** 鉴权与读取完全同一套（BFF 注入 `Authorization` + 转发 `third-session` +
  `X-Business-Entry`），不需要新权限、新 scope。
- **响应体与轮询响应是同一个形状**（同一个序列化函数）：取消成功后**直接拿到该次作业的终态，
  不必再轮询一次**。
- **为什么是 `POST .../cancel` 而不是 `DELETE /{qa_id}`**：作业行要保留（审计与历史），
  `DELETE` 会暗示资源已被移除，与保留语义冲突。前端**不要**在取消后把 `qa_id` 从本地历史里删掉。

### 幂等语义（停止按钮是网络抖动下会被重复触发的入口）

| 情况 | HTTP | `status` | 说明 |
|---|---:|---|---|
| 作业在飞（`queued`/`running`） | 200 | `cancelled` | 正常取消 |
| 连点两次 | 200 | `cancelled` | 第二次同样 200 |
| 取消一个恰好已完成的作业 | 200 | `completed` | **完整答案照样返回**，取消不改写结果 |
| 取消一个已失败的作业 | 200 | `failed` | 返回它自己的失败态 |

**一律不报错。** 前端不需要为取消做重试、降级或错误提示；出现非 200 只有下面一种原因。

### 404 `QA_NOT_FOUND` 的三种同形原因

`qa_id` 不存在、**不属于当前调用者**（同一调用主体下的另一个作用域，即另一个 scope）、已过保留期
被清扫 —— 三种情况返回**同一个** `404 QA_NOT_FOUND`。这是刻意的：取消不能成为探测
「哪些 `qa_id` 真实存在」的通道。**不要**拿它区分「作业不存在」和「取消失败」。

> 例外（不在 BFF 处理范围内，但联调时会遇到）：身份层**先于**作业查询拒绝「身份映射跨租户」
> （`PlatformIdentityResolver.resolve` 抛 `identity mapping crosses tenant boundary`，
> 网关映射为 `503 PLATFORM_UNAVAILABLE`、`retryable=true`），因此那种调用方连 `404` 都拿不到 ——
> 它不会走到查询作业这一步，所以它同样学不到任何信息。前端把 `503` 当瞬时错误提示即可，
> **不要**当成取消失败去重试。

### 正确性不依赖中断成功

取消是**落库优先 + 尽力中断**：先把作业行写为 `cancelled` 终态（这一步成功即宣告正确性成立），
再尽力中断底层正在执行的模型轮次以省下 token。中断失败只影响省下多少 token，**不影响终态**。
所以前端可以放心把「取消返回 200」当作「已经停」。

---

## 4. `cancelled` 的界面含义：**无内容**

- **这个接口不流式。** 严格「请求 + 轮询」，模型在一个轮次结束时一次性返回完整答案。所以后端在被
  取消的那一刻手里只有两种状态：**一个字节都没有，或者答案已完整落库**。**不存在"半截答案"**。
- 因此 `status=cancelled` 时 **`result` 恒为 `null`、`error` 恒为 `null`**（见 §5 样例）。
- **不要实现「渲染已生成的部分内容」分支**——它会永远为空。界面显示「已停止」，不带内容气泡。
- **作业行保留**（终态 `cancelled`，供审计与历史）；**会话轮次行不保留**：被停止的那次提问不出现在
  `GET /v1/conversations/{id}` 的 `turns` 里，也不会成为后续推理的事实依据（用户否掉的回答
  不能当上下文）。
- **极小窗口竞态**：答案已算出、尚未提交时用户点了停止 —— 一律丢弃，与「取消不入上下文」一致。
  用户点停止就是因为不想要这个回答。
- **保留期**：`cancelled` 走失败档 **5 分钟**。刷新窗口内可查，之后轮询与取消都得到 `404`。
  「已停止」那一帧的内容由**前端本地持有**，不要指望刷新后从后端再取回一次。
- 语义强度上它与「失败」无关：**不是失败、不是没有发生过、也不是超时**。

---

## 5. 真实响应样例

下列响应体由 `tests/test_assistant_cancel_handoff.py` 在**真实网关栈**上抓取（真实
`GatewayRuntime` + 真实 `GatewayStore` + 走 HTTP 路由的 `TestClient`，只把模型调用停在半路以复现
「用户正在等待时按下停止」这个时刻）。只有逐次生成的 `qa_id` / `conv_` 前缀 id 与 ISO 8601
时间戳被换成占位值——**其余逐字段等于真实响应，测试会比对，不一致即失败**。

### 样例 1：创建提问进入等待态 —— `202`

<!-- contract-sample: ask-202 status=202 -->

```json
{
  "type": "qa",
  "language": "zh",
  "qa_id": "qa_c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6",
  "question": "电动车的电池保养怎么做",
  "status": "queued",
  "retry_after_ms": 1000,
  "result": null,
  "error": null,
  "conversation_id": "conv_d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801",
  "turn_no": 1
}
```

要点：`status=queued` + `retry_after_ms=1000` → **进等待态**。`conversation_id` / `turn_no`
**只有带 `conversation_id` 提问时才出现**；不带会话的提问没有这两个字段。

### 样例 2：用户按下停止 —— `200`

<!-- contract-sample: cancel-200 status=200 -->

```json
{
  "type": "qa",
  "language": "zh",
  "qa_id": "qa_c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6",
  "question": "电动车的电池保养怎么做",
  "status": "cancelled",
  "retry_after_ms": null,
  "result": null,
  "error": null
}
```

要点：**取消响应就是终态响应**——`retry_after_ms` 已为 `null`，不需要再轮询一次。
注意它**不带** `conversation_id` / `turn_no`（那两个字段只属于 202 创建响应），不要依赖它们做取消后的界面判断。

### 样例 3：取消后再轮询同一个 `qa_id` —— `200`

<!-- contract-sample: poll-cancelled status=200 -->

```json
{
  "type": "qa",
  "language": "zh",
  "qa_id": "qa_c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6",
  "question": "电动车的电池保养怎么做",
  "status": "cancelled",
  "retry_after_ms": null,
  "result": null,
  "error": null
}
```

要点：与样例 2 **逐字段相同**——轮询与取消本来就是同一个形状，客户端读一个已停止的作业和读一个
已完成的作业用的是同一套代码。取消后若前端仍有轮询在飞，它会在这里自然停下。

### 样例 4：会话槽位随终态一起释放

提问后（`is_generating` 为 `true`，且轮次行已经在库里、`answer` 为空）：

<!-- contract-sample: conversation-busy status=200 -->

```json
{
  "conversation_id": "conv_d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801",
  "business_entry": "consumer",
  "agent_version_key": "agt_abcdef1234567890#v1",
  "active_order_no": null,
  "is_generating": true,
  "created_at": "<ISO 8601 时间戳>",
  "updated_at": "<ISO 8601 时间戳>",
  "expires_at": "<ISO 8601 时间戳>",
  "turns": [
    {
      "turn_no": 1,
      "kind": "qa",
      "question": "电动车的电池保养怎么做",
      "answer": null,
      "created_at": "<ISO 8601 时间戳>"
    }
  ]
}
```

取消后，同一个会话：

<!-- contract-sample: conversation-free status=200 -->

```json
{
  "conversation_id": "conv_d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801",
  "business_entry": "consumer",
  "agent_version_key": "agt_abcdef1234567890#v1",
  "active_order_no": null,
  "is_generating": false,
  "created_at": "<ISO 8601 时间戳>",
  "updated_at": "<ISO 8601 时间戳>",
  "expires_at": "<ISO 8601 时间戳>",
  "turns": []
}
```

要点：**`turns` 变空数组**——被停止的轮次一行都不留，因此它永远不会成为后续追问的上下文。
会话随即可以接受新提问（不会 `409 CONVERSATION_BUSY`）。

### 样例 5：取消一个不存在 / 不属于我的作业 —— `404`

<!-- contract-sample: cancel-404 status=404 -->

```json
{
  "error": {
    "code": "QA_NOT_FOUND",
    "message": "assistant question not found",
    "retryable": false
  }
}
```

要点：与「作业已过保留期」「作业属于别的租户」**完全同形**，`retryable=false`。前端统一提示
「未找到」即可，不要做探测，也不要重试。

### 样例 6：同步分支对照 —— `faq` 命中是 `200`，**不进等待态**

<!-- contract-sample: faq-200 status=200 -->

```json
{
  "platform": "consumer",
  "available_platforms": [
    "consumer",
    "operator"
  ],
  "type": "faq",
  "language": "zh",
  "faq_version": "2026.09.12",
  "question_id": "consumer.faq.q017",
  "question": "充电前支付的预授权/预充值金额，什么时候退回？",
  "answer": "根据您选择的支付方式，退款到账时效如下：\n使用充值钱包（先充后付）：预先冻结资金，充电结束后直接按实际消费金额扣款；\n平台账户钱包余额：充电结束扣除实际发生费用，多退少补；钱包内的剩余可用余额支持随时在【我的钱包 - 申请退款】全额原路提现，1～3 个工作日内退回原支付账户；",
  "format": "text"
}
```

要点：没有 `qa_id`、没有 `retry_after_ms`、`type=faq` + `200` → **不锁输入框、不轮询、不出现等待动画**。
把同步响应套上等待态，用户会看到界面闪一下，以为出了故障。

---

## 6. 陷阱清单

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | BFF 没放行 `/cancel` 路径 | §0.1。未放行是稳定的 404/405，与「取消失败」同形——先确认再联调 |
| 2 | BFF 用词表白名单丢掉了 `cancelled` | §0.2。新增状态值必须**原样透传**，不得静默改写或丢弃 |
| 3 | 给 `faq` / `clarification` 也套等待动画 | §1。判别只有 `type` + HTTP：同步 `200` 永远不锁 |
| 4 | 自己加一个客户端超时（如 30 秒）解锁 | §2。解锁唯一权威是作业终态；后端已有 15 分钟 deadline 与 120 秒槽位兜底 |
| 5 | 用 `is_generating` 做解锁条件 | §2。它是会话并发闸门，可作展示 / 刷新恢复；权威是作业 `status` |
| 6 | 实现「渲染已生成的部分内容」 | §4。接口不流式，**不存在半截答案**，该分支永远为空 |
| 7 | 取消后把 `qa_id` 从本地历史里删掉 | §3。作业行保留为 `cancelled` 终态，供审计与历史 |
| 8 | 取消失败就重试 / 弹错误 | §3。取消幂等，只要不是 404 就是 200 |
| 9 | 拿 `404 QA_NOT_FOUND` 区分「不存在」与「取消失败」 | §3 / 样例 5。三种原因同形，且是刻意的 |
| 10 | 刷新后期望还能从后端取回「已停止」那一帧 | §4。`cancelled` 只保留 5 分钟，内容由前端本地持有 |
| 11 | 把重启造成的 `failed` 显示成「已停止」 | §2。它是 `QA_INTERRUPTED_BY_RESTART`，是发版而不是用户取消 |
| 12 | 取消响应里找 `conversation_id` / `turn_no` | §5 样例 2。只有 202 创建响应带这两个字段 |
| 13 | 给跳转类快捷动作套等待态 | §1。它不走统一入口，没有响应 `type` |
| 14 | 照抄 QA 的停止按钮给诊断线 | §1.1。诊断进入等待态但**没有取消路由**，按钮永远 404/405 |
| 15 | 诊断等不下去时让前端假装「已停止」 | §1.1。那是本地骗自己：后端作业仍在跑。只能用不承诺中断的动作 |
| 16 | 把诊断 id 传给 `/v1/assistant/questions/{id}/cancel` | §1.1。两者是不同资源与 id 空间，只会得到 404 |

---

## 7. 这份契约的回归守护

样例不会说谎，是因为有一条测试把文档和网关绑在一起。下列测试「去掉实现即失败」已实测：

| 守护 | 文件 | 守护的行为 |
|---|---|---|
| `test_the_handoff_shows_what_the_gateway_answers` | `tests/test_assistant_cancel_handoff.py` | §5 全部样例逐字段等于真实响应——文档漂移即测试失败 |
| `test_stopping_a_question_returns_its_terminal_state_and_unlocks_the_conversation` | `tests/test_assistant_api.py` | 取消返回终态、会话槽位立即释放、后续提问不再撞 409、中断到达活的模型轮次 |
| `test_stopping_a_question_twice_and_after_it_finished_is_not_an_error` | `tests/test_assistant_api.py` | 连点两次 / 取消已完成作业一律 200，且不改写已完成的结果 |
| `test_stopping_an_unknown_or_foreign_question_is_404` | `tests/test_assistant_api.py` | 未知与跨作用域同为 404，且作业状态不变 |
| `test_a_failed_interrupt_still_cancels_the_job` | `tests/test_assistant_qa_cancel.py` | 中断抛错时作业仍然是 `cancelled`（正确性不依赖中断成功） |

「停止生成」的可验证据就是上表加 §5 样例。此前三份文档把它归给已关闭的 #173，而 #173 的验收条目
在范围决定时被降级为范围外、2026-09-14 的 PASS 证据引的是 CONV-01（一个不含任何停止接口的会话
生命周期用例）——**停止生成当时从未有过实现或证据**。该断链已在本单纠正，相关引用统一指向
PRD #346 与本文。

---

## 8. 联调前请做的一次真实验收

§5 的样例来自本地真实网关栈，**尚未在 41 公网链路跑过取消**。联调时按下面三步走一遍，即可把
「未完成业务验收」这个缺口补上（结果请回到 `docs/validation.md` 记录，不要写在本文里当已验收）：

1. 用有效 C 端会话提问一个自由问题，确认 `202` + `retry_after_ms=1000`；
2. 立刻 `POST /v1/assistant/questions/{qa_id}/cancel`，确认 `200` + `status=cancelled` + `result:null`；
3. 同一个会话再提一问，确认**不再** `409 CONVERSATION_BUSY`，且 `turns` 里没有被停止的那一轮。

只带 `third-session` + `tenant-id`，不要带 `Authorization`（公网 nginx 注入服务令牌），
`third-session` 必须全小写连字符。BFF 放行 `/cancel` 之前，第 2 步会稳定 404/405。

---

## 9. 相关文档

- 统一助手入口完整契约（FAQ / QA / 诊断 / 澄清 / 快捷动作）：[frontend-api-brief.md](./frontend-api-brief.md) §2.3（状态值）与 §10.8（调用示例）。
- 跳转类动作不锁输入框的交接：[frontend-jump-path-handoff.md](./frontend-jump-path-handoff.md)。
- 后端实现取舍：PRD #346；取消端点在 #357，`cancelled` 终态在 #354，重启收敛在 #356。
- 验证记录与已知缺口：[`../validation.md`](../validation.md)。
