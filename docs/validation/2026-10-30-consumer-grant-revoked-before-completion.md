# 终态可读前先撤销取数凭据（CI 抖动定位与修复）

日期：2026-10-30。分支 `fix/consumer-grant-revoked-before-completion`。

## 现象

同一份**只改文档**的 PR（#1228）连续两次 CI 红在两个**不同**的用例上：

| 轮次 | 失败用例 | 断言 |
| --- | --- | --- |
| 1 | `tests/consumer/test_consumer_model_reply.py:172` | `assert refused.status_code != 200` 得到 200 |
| 2（rerun） | `tests/consumer/test_consumer_knowledge_flow.py:136` | `assert ...by_token(...).state == "revoked"` 得到 `consumed` |

本机对同一份代码：单文件 `-n 0` 连跑 3 次、`tests/consumer` 整体 `-n 4` 连跑 2 次，
全部通过。⇒ 不是 PR 内容引起的，是**时序**。

## 定位

`consumer_chat_service.execute` 的收尾顺序是：

```python
stored = runtime.store.update_assistant_question(qa_id, status="completed", result=answer)
...
finally:
    DataGrantStore(...).revoke_for_qa(qa_id)   # 撤销这次请求的数据凭据
```

`status=completed` 先可读，凭据后失效。任何按状态轮询的读方（回读接口、验收脚本、
客户端）都可能落在这个窗口里，看到"结果已交付，而这次请求的取数凭据仍然可用"。
测试只是把这个窗口放大到能观测：`done()` 轮询到 completed 就断言凭据已撤销，
命中窗口时读到 `consumed`（工作流回调时消耗，尚未撤销）。

## 修复

**先撤销取数能力，再公布终态。** `revoke_for_qa` 是幂等的，`finally` 里那一份保留为兜底
（异常路径仍然撤销）。失败/取消路径的顺序不变。

## 新增回归

`test_data_grant_is_already_revoked_when_the_result_becomes_visible`：把观察点钉在
**写终态的那一次 `update_assistant_question(status="completed")` 调用内部**，
断言此刻凭据状态已经是 `revoked`。它不依赖调度时序，所以不会自己变成新的抖动源。

## 验证

```bash
uv run pytest -n 0 tests/consumer/test_consumer_knowledge_flow.py   # 11 passed
uv run pytest -n 4                                                  # 3626 passed, 9 skipped
uv run ruff check src tests && uv run ruff format --check src tests
```

`deploy/m2-live-03/images.lock.json` 重建写回（`source_sha256=397d4b18…`，`built_at=2026-10-30T13:29:55Z`）。

## 边界

- 另一个抖动（`test_consumer_model_reply.py:172`）**没有**在本片修复：它的失败形态是
  "被篡改的正文仍然以 200 返回"，与本片的凭据窗口不是同一件事。本机 3/3 通过，
  原因未定位 —— 如实记在这里，不宣称已修。
- 本次只调整顺序，没有改变"哪些路会撤销凭据"。
