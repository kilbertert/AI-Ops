# invalid document answer：记录形态而不是原文（2026-11-01）

生产版本：`1.0.0+9e456e918181`（本改动未部署）。

## 为什么不是"附上原始输出"

要求是让下一次失败可取证。原文是**模型生成的用户可见文本**，可能含订单号、金额、
站点名；`reject_detail` 会进运维表，也会被回读接口带到响应里。直接存原文等于
把一次失败的用户数据写进排障记录。

排障真正需要的是**形态**：模型是没按 JSON 答（纯文本），还是答了 JSON 但被
markdown 围栏包住，还是 JSON 被截断。这三者的修法完全不同（改协议块 / 放宽解析 /
调 `max_tokens`），而形态本身不含任何用户数据。

## 改了什么

`consumer_documents._json_failure_shape(raw)`：

| 输入形态 | 记录值 |
|---|---|
| 非字符串 | `not_text:<类型名>` |
| 空白 | `empty` |
| 以 ```` ``` ```` 开头 | `fenced` |
| 以 `{` / `[` 开头但解析失败 | `json_syntax` |
| 其它纯文本 | `plain_text:<字符数>` |

只在 `json.loads` 抛错处使用，其余五条 `invalid document answer …` 的判据原样保留
（字段不符 / 取值不符 / notice 种类不符），它们本身就已经说明了原因。

```python
except (ValueError, TypeError) as exc:
    raise ChatRunUnavailable(
        "CHATFLOW_ANSWER_INVALID", f"invalid document answer ({_json_failure_shape(raw)})"
    ) from exc
```

## 验证

```
uv run pytest -q tests/tooling/test_m2_live03_deploy.py tests/consumer/  → 全通过
uv run ruff check / format                                              → 通过
python deploy/m2-live-03/context_digest.py --check                      → match: true
```

新增回归 `test_non_json_answer_reports_shape_without_leaking_text`：五类形态各有断言，
并断言 `"充电" not in shape` —— 如果哪天有人把原文拼进返回值，这一条转红。

## 边界

- 仍是取证能力，不是业务行为：**没部署**，所以生产上还没有一条带形态的
  `reject_detail`。
- 部署后重跑订单查询（真实用户会话 `07b9229f…`，订单 `2109928861763776513`），
  读到的形态决定下一步：`fenced` → 解析端容忍围栏；`plain_text:*` → 协议块的
  JSON 约束不够硬；`json_syntax` → 多半是 `max_tokens` 截断。

**业务验收未完成。**
