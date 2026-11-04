# 2026-11-04：凭据判据两种形状一个答案（#787）

**里程碑目标**：`redaction.py` 内部「什么算凭据」按数据形状分叉，同一批字段名在映射键
与自由文本里得到相反答案；URL userinfo 另有两套实现。收敛成一处定义。

## 已完成内容

| 项 | 改前 | 改后 |
|---|---|---|
| 自由文本名字 | `\b(password\|secret\|token\|api[_-]?key)\b`，`_` 处不成立 | 整段标识符匹配，`inside_token=` 同答案（ADR-0210） |
| `signature_key` | 两条判据都漏（只靠 `repr=False`） | 映射键与自由文本都判为凭据 |
| URL userinfo | `redact_text` 只认 `https?://`；`config` 另有 urlsplit 实现 | `redact_url_credentials()` 一处定义，任何 scheme |
| 截断 | `[:1000]` × 4、`[:500]` × 3、`...[TRUNCATED]` 两处 | `ERROR_TEXT_LIMIT` / `ERROR_TEXT_SHORT_LIMIT` / `TRUNCATION_MARKER` / `truncate()` |

**行为变更（显式列出）**：`inside_token=` / `service_token=` / `client_secret=` /
`access_token=` / `csrf_token=` / `integration_api_key=` / `signature_key=` 在自由文本中
改为被抹除；`redis://` / `mysql://` 等任意 scheme 的 userinfo 在 `redact_text` 与
`Settings.redacted()` 两处得到同一结果。`preserve`（订单号）策略**未改**。

## 验证结果

- 新增 `tests/common/test_redaction.py`（20 条）：七个字段名 × 两种形状同一答案、
  `signature_key`、普通散文不被误伤、四种 scheme 的 userinfo、两条 URL 路径同结果、
  查询串里的 `@` 不被误判、截断标记只一处、`preserve` 仍然生效。
- `uv run pytest tests/common tests/gateway tests/dify`：1410 passed。

**真实故障业务验收：未完成** —— 本轮证明的是「同一份输入不再得到两个答案」，
未比对线上已持久化的历史数据（本轮不做数据订正）。

## 已知缺口

- **未部署**。
- 线上历史上已持久化的 `inside_token=…` / `redis://u:p@…` 文本不会被本轮改写；
  若需要订正，是一次显式的数据操作，不在本 PRD 范围。
- `config.py` 的逐字段 `"REDACTED" if ... else ""` 清单未收进 `is_sensitive_name`
  —— 它按**值**判空，不是按名字判敏感，语义不同。
