# 失败台账校正与误拒修复（2026-11-03）

承接指示：继续收尾，**先取证，不要直接强化"第三行禁止状态"的提示词**。
基线 `98e8ee2a`，生产 `1.0.0+c54735dbaa6a`。

---

## 一、校正失败台账

### 差异是真的：我上一份回执把次数算错了

上一份回执写「8 次调用、1 成功 / 7 失败」，却列了 `json_parse 3 + status 3 + EVIDENCE_MISSING 2`
合计 8 条。**多出来的那一条不属于订单批次。**

日志**不带 run_id / qa_id**，只能按完成时间对齐。逐条对完：

| # | run_id | 完成（本地） | 状态 | 清单 | 日志事件 | 属于本批 |
|---|---|---|---|---|---|---|
| 1 | `run_ptfanR0Ozd…` | 19:56:01 | failed | v2 | `EVIDENCE_MISSING` | 是 |
| 2 | `run_ecG_q5S30G…` | 19:58:26 | failed | v2 | `EVIDENCE_MISSING` | 是 |
| 3 | `run_5-xDn8WgR9…` | 19:59:55 | failed | v2 | `unsupported_fact detail=status line=3 len=102` | 是 |
| 4 | `run_L99V6WGODc…` | 20:01:28 | failed | v2 | `json_parse` | 是 |
| 5 | `run_6UnyDCziQX…` | 20:03:16 | failed | v2 | `unsupported_fact detail=status line=3 len=155` | 是 |
| 6 | `run_gDC9ifogfh…` | 20:04:07 | failed | v2 | `unicode/other` | **否**（问句是「我有哪些站点？」） |
| 7 | `run_z_KdnLaxKH…` | 20:04:39 | failed | v2 | `unsupported_fact detail=status line=3 len=101` | 是 |
| 8 | `run_VMA2x8czO2…` | 20:05:35 | failed | — | `OPERATOR_INVALID_ANSWER` | **否**（站点） |
| 9 | `run_PkRwm-9bed…` | 20:06:50 | **succeeded** | v2 | — | 是 |
| 10 | `run_m7qa2Sd2B5…` | 20:09:08 | failed | v2 | `EVIDENCE_MISSING` | 是 |

**订单批次（同一句原问法）= 8 次：1 成功 / 7 失败**，分布：

| 原因 | 次数 |
|---|---|
| `OPERATOR_EVIDENCE_MISSING` | 3 |
| `unsupported_fact detail=status line=3` | 3 |
| `json_parse` | 1 |
| **合计** | **7** ✓ |

### 台账的可靠边界（必须写明）

- 日志**没有 run_id**，上表是**按完成时间**对齐的，不是按 id 关联。
  20:04:07 那条 `json_parse` 我原本算进了订单批次，错就错在这里。
- 三条 `EVIDENCE_MISSING` 与 site 的两次请求时间接近，**无法 100% 排除**是站点请求；
  但 19:56:01 / 19:58:26 / 20:09:08 三次的完成时刻都精确对上订单 run，
  且站点 run 的完成时刻（20:04:07 / 20:05:35）另有归属，所以归入订单批次。
- **无法关联到 run 的日志一律不计入分布。**

### 清单版本

该批 10 条 run 的 `execution_manifest` **全部是 v2（旧契约）**。
也就是说：这批失败与「#1307 的数字禁令」无关 —— 那些数字禁令根本不在跑的清单里。

---

## 二、三条失败输出的原文：**不可恢复**

`operator_cpms.complete()` 只在**成功路径**把 `answer` 写进 snapshot（`operator_cpms.py:787`）；
拒绝走 `store.fail()`，不写 answer。实测确认失败 run 的 `snapshot_json.answer` 恒为 `None`。

**结论：那三条 reply 的原文已不可恢复。** 不猜测、不重建、不冒充历史证据。
下文第三节的三条成因是**读代码 + 构造样本复现**得出的，不是从历史 run 读出来的；
若需要真实样本，只能重新取样，并明确标成**新样本**。

---

## 三、已复现的误拒（并已修）

### 缺陷

2026-11-02 我加的那条是「整段出现结算类词即拒绝」（`_settlement_hit`）。它过头了：

| 输入 | 修前 | 应该 |
|---|---|---|
| `请先核对账单。` | **拒绝** | 通过（核对建议） |
| `目前没有结算状态的证据。` | **拒绝** | 通过（缺证说明） |
| `系统未提供结算状态字段。` | **拒绝** | 通过 |
| `您想核对账单的哪一项？` | **拒绝** | 通过（提问） |
| `这单尚未结算。` | 拒绝 | 拒绝 ✓ |
| `该订单已经结算完成。` | 拒绝 | 拒绝 ✓ |

八条正例全红（实测）。**我们要求模型写「缺证说明」，却把「没有结算状态的证据」判成违规** —— 这是自相矛盾。

### 修法：判**断言形式**，不判词是否出现

只改 `consumer_facts` 这一处，两个调用链（消费者端 `consumer_documents`、管家端
`operator_cpms._validate_reply_facts`）同时生效。

- 三组断言形式：① 结算+状态/动作（已结算/尚未结算/结算中）；② 已|未|仍在 + 结算；
  ③ 结算状态是/为…
- 先剥掉两类**放行**形式：问句（`？`/A-不-A），以及「没有证据」类说法。
- **不删整类校验**：充电/完成状态那一套判据原样保留。

### 验证

```
tests/consumer/test_prompt_fact_segments.py   14 passed（8 正例 + 6 反例）
tests/consumer + test_operator_reply_facts   590 passed
```

**双向变异验证**（这是这条修法唯一站得住的理由）：

| 变异 | 结果 |
|---|---|
| 退回「出现即拒」 | **7 条红**（正例被误拒） |
| 完全关掉结算判断 | **5 条红**（断言被放行） |

收紧和放松两个方向都被用例钉住，不是「给这一句话加特例」。

---

## 四、另外两类失败

### `json_parse`（本批 1 次）

**不能直接等同于「模型没吐 JSON」。** 该码覆盖四种形态：空 / 被围栏包住 /
形似 JSON 但截断 / 纯文本。此前日志只有码，四种分不开。

已在该拒绝点补上形态（**复用** `consumer_documents._json_failure_shape`，
它本来就为同一个缺口写过，只记形态与长度、不记原文）：

```
operator_answer_rejected reason=json_parse empty
operator_answer_rejected reason=json_parse fenced
operator_answer_rejected reason=json_parse json_syntax      ← 截断
operator_answer_rejected reason=json_parse plain_text:213
```

**仍未知**：那 1 次的真实形态 -- 原文没保存。新取样才能确定。

### `OPERATOR_EVIDENCE_MISSING`（本批 3 次）

**该码有四个触发点**，不能一律归因为「漏引用」：

| 位置 | 情形 |
|---|---|
| `operator_cpms.py:178` | 站点证据缺失 |
| `operator_cpms.py:698` | 节点回调里 `expected is None` 或授权 id 不符 |
| `operator_cpms.py:1134` | **回答校验**：没取到 / 没引用 / 引了不存在的工具 |

已给第 1134 处（就是本批这三次的路径）加区分度，判决不变：

```
operator_evidence_missing detail=not_read        ← 订单详情这轮根本没取到
operator_evidence_missing detail=not_cited       ← 取到了，模型没在 evidence_tools 里引
operator_evidence_missing detail=unknown_tool name=…
```

**仍未知**：那 3 次属于哪一种。同上，原文没保存，要新取样。

---

## 五、对「第三行禁止状态」的判断（**暂不动手**）

证据只支持到这一步：

- 3 次 `unsupported_fact` 都命中 `check_status_prose`，都在 `reply` 第 3 行，
  长度 102 / 155 / 101 字符；
- **但那三行写的是什么，不可恢复。**

所以现在改提示词等于**基于猜测改协议** —— 这正是 #1307 犯过的错（它假设「模型写了数字」，
实际命中的是状态检查）。按指示，先取证再动。

---

## 六、证据强度的更正：接口缺字段这件事

上一份回执写「真实响应 34 个字段里没有 settlement 字段」。**这个论证不成立。**

`project_evidence()` 是按 `evidence_fields` **投影**过的，投影后的 evidence 没有该字段，
只能证明「投影里没有」，**不能独立证明原始接口没有**。

同样地，`occupyFee = null` 也只能说「这一单上游给了 null」，
**不能仅凭数值推断为「不适用」** —— 需要接口文档或业务语义依据。

两处都已在文档里改成这个更弱的说法；结算状态与关联占枪费订单继续列为**未完成**。

---

## 七、本轮改动

| 文件 | 改动 |
|---|---|
| `consumer/consumer_facts.py` | 结算类判据从「出现即拒」改为「断言形式」 |
| `operator/operator_cpms.py` | `EVIDENCE_MISSING` 四情形区分；`json_parse` 记形态 |
| `tests/consumer/test_prompt_fact_segments.py` | 8 正例 + 6 反例 |
| `tests/operations/test_operator_reply_facts.py` | 两个错误码的区分度 |

**尚未部署**；本轮未改任何提示词文本。

---

## 八、本轮身份冻结（截至 2026-11-03 取证时刻）

验收前必须能回答「跑的到底是哪一份」。以下全部取自 41 现网库，不是记忆：

| 项 | 值 |
|---|---|
| Gateway 版本 | `1.0.0+c54735dbaa6a`（`/health`） |
| operator 路由 | `revision = 16`，`agent_version_no = 1` |
| 绑定 app_id | `2b0629cf-bb84-5e1f-bdd8-8b1da2ce9053` |
| 清单 v1 | `manifest_sha256 = 6b9ac7b9340678ef7b26a76f9b3f984f88d8f2208dbb31b7761cadda3e51b52c` |
| `system_messages[0]` | 890 字，`sha256 = 353f4ca1…8a2d63` |
| 生效契约 | **v2（旧）** —— 不含「一个字都不要带数字」 |
| 模型 | `deepseek-v4-flash`（adapter `openai-compatible`，endpoint `model-proxy:9101`） |
| `model_options` | `{}` |
| `history_policy` | `none` |
| 工具 | `cpms_order_detail`、`cpms_charging_samples` |

**注意**：该指针（v1 / rev16）不是本次会话设置的，来自 2026-11-02 12:04 之后的某一轮。
本轮只核对，**没有改它**。

**本轮代码改动的生效边界**：

- `consumer_facts.check_status_prose` 与 `operator_cpms` 的两处诊断 —— 走 **Gateway 代码路径**，
  部署 Gateway 即生效，**不需要重新发布节点清单**。
- 原因：两个调用点（`consumer_documents.render_segments`、`operator_cpms._validate_reply_facts`）
  都是对**模型本轮输出**的校验，不在已发布文案里。
