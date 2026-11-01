# 尾随 `"}"`：验证记录（2026-11-01）

生产版本：`1.0.0+833e15b7b5d8`（本改动未部署）。

## 根因（实测，不是推断）

同一版提示词下，模型有约三分之一的概率在**正确的 JSON 对象之后**多吐一个 `"}`。
对象本身完整、字段全对；坏掉的只有结尾。

通过的那条：

```
{"kind":"realtime","answer":"","reference_ids":[],"reply":"{{fact:status_line}}\n{{fact:observed_line}}\n如需核对更多细节，可以告诉我订单号或具体想了解哪一项。"}
```

失败的四条（14:42 / 14:39 / 10:22 / 08:18，跨两台不同会话、两个订单）：

```
{"kind":"realtime","answer":"","reference_ids":[],"reply":"{{fact:status_line}}\n{{fact:observed_line}}"}"}
```

取证路径：`json_syntax` 形态（#1291）→ Dify console 的
`chat-conversations` / `chat-messages` 接口逐条 `json.loads` → 12 条里 4 条不通过，
四条尾巴逐字相同。**与截断无关**：失败条目比通过的最短成功条目还短。
**与提示词无关**：提示词是同一份，14:37 的成功与 14:42 的失败隔着 30 秒。

## 改了什么

`consumer_documents.load_selection`：

1. 先按原样 `json.loads`。
2. 失败时用 `_object_end`（识别字符串内外的括号）扫出**开头那个完整对象**的结尾。
3. 只有当"对象之后剩下的全是空白与标点"（`_TRAILING_GARBAGE`）才接受它。

**同一份输出还有两个解析点**，一起改：`consumer_chat_service` 存 `reply_source`
的那一处、`consumer_knowledge._has_reply`。只改一处的话，宽容了解析却存不下正文，
或者正文退回固定文案 —— 同一个成因换了个地方失败。

拒绝的边界同样重要：`{"kind":"x"} 好的`、`好的{"kind":"x"}`、半截对象、纯文本
仍然整轮失败 —— 那些不是格式手滑，是没按协议答。放宽它们会让"把正文当 JSON 交"
变成可以接受的行为。

## 验证

```
tests/consumer/test_prompt_fact_segments.py             → 29 passed
  新增 test_a_complete_selection_followed_by_stray_punctuation_is_still_used
  新增 test_tolerance_does_not_excuse_an_answer_that_is_not_a_json_object
变异验证：把兜底关掉（`end = None`）→ 第一条转红；
          把 `answer_selection` 改回严格 `json.loads` → 第一条转红；
          边界测试在两种变异下都保持绿（它守的是"不许放宽"）。
context_digest.py --check                               → match: true
```

## 边界

- 没部署，生产上仍会以原样拒绝这 1/3 的答复。
- 补救只覆盖"对象 + 标点尾巴"。若下一步出现别的形状（例如围栏 ```` ``` ````），
  `reject_detail` 会记 `fenced`，届时再决定是否值得放宽 —— 不在没有样本时预设。

**业务验收未完成。**
