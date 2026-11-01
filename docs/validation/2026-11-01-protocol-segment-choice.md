# 协议块"只引用本轮实际给出的段"：验证记录（2026-11-01）

生产版本：`1.0.0+56995d3d1153`（**本改动未部署、生产 Dify 工作流未迁移**）。

## 改了什么

`consumer_documents.PROTOCOL_BLOCK` 在"账单必须引用billing_line，状态必须引用status_line"
之后补一句：

```
**只引用本轮实际给出的段**：看不到billing_line就不要写它（那是账单查询），
看不到status_line也不要写它（那是状态查询）。两者都在时先写billing_line。
```

## 验证结果

```
tests/consumer/test_prompt_fact_segments.py   → 31 passed
  新增 test_protocol_says_to_use_only_the_segments_this_turn_actually_has
全量 pytest -q                                → 退出码 0
ruff check                                    → 通过
context_digest.py --check                     → match: true（rebase 后按实际树重算）
```

## 这条测试能证明什么、不能证明什么

能证明：协议块里确实出现了这条约束（有人删掉它，测试转红）。
**不能证明**：模型不再引用缺失的段。那是模型行为，只能靠部署后真实订单复测。

## 未完成的迁移（这是本 PR 的实际缺口）

模型运行时读的是 Dify 工作流里**保存的** SYSTEM 文本，不是这个 Python 模块。
所以本改动单独合并**不会改变生产行为**。完整路径：

1. `tools/content/upgrade_consumer_flow.py --live-documents` 迁移现役工作流的协议块
   （只替换带 `<!-- aiops-protocol:` 标记的那一块，运营文字逐字保留）。
2. 经 `POST /v1/dify/publish` 发布，使 `callback_version` 从新内容冻结。
3. 同一订单连续 ≥10 次真实会话，确认 `CHATFLOW_FACT_UNKNOWN` 消失。

## 观测基线（部署 56995d3d 后）

| 时间窗 | 成功 | 失败 | 失败形态 |
|---|---|---|---|
| 15:50–15:54 | 6 | 3 | 全部 `CHATFLOW_FACT_UNKNOWN / required evidence segment missing` |

**业务验收未完成。**
