# reject_detail：验证记录（2026-11-01）

生产版本：`1.0.0+9e456e918181`（本改动未部署）。

## 改了什么
| 文件 | 改动 |
|---|---|
| `dify_run_store.py` | 新增 `reject_detail` 列；`fail()` 增加可选 `detail` |
| `consumer_chat_service.py` | `ChatRunUnavailable` 失败时传 `str(exc)[:200]` |
| `gateway_runtime.py` | `_settle_dify_failed` 增加 `detail`；订单链路走的正是这条 |

未改 `reason` 语义：它仍是稳定错误码。全仓有三处按 `reason` 精确断言的测试
（`consumer_knowledge_picker`、`m2_close_contract`、`app_runtime_dify_entry`），
本次全部保持通过 —— 这是"不动 `reason`"的机械证明。

## 验证结果
```
uv run pytest -q tests/ -n 4   → 3830 passed / 9 skipped / 0 failed
ruff check / format --check     → 通过
context_digest.py --check       → match: true
```

新增回归：
`test_failure_reason_records_the_specific_judgement_not_only_the_code`
断言同一场景下 `reason == "CHATFLOW_ANSWER_INVALID"`（不变）
且 `reject_detail == "required evidence segment missing"`。
**变异验证**：撤掉 `fail(detail=...)` 后该测试转红。

## 边界
- 这是**取证能力**的验证，不是业务行为验证：没有部署，因此没有一条真实生产失败记录携带 `reject_detail`。
- 部署后需重跑订单查询才能回答"提示词不明确 vs 守卫过严"。

**业务验收未完成。**
