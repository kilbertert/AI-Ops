# 2026-11-04：凭据判据两种形状一个答案（#787）

## 目标

`redaction.py` 里「什么算凭据」按数据形状分叉：映射键认 `inside_token`，自由文本不认；
URL userinfo 有两套实现。收敛成一处，URL 部分走 `urlsplit`。

## 已完成内容

- `CREDENTIAL_ASSIGNMENT` 改为整段标识符匹配；`SENSITIVE_KEY` 补 `signature_key`。
- 新增 `redact_url_credentials()`（任何 scheme，`urlsplit`）；`config._redact_url_credentials`
  变成一行薄壳。
- 截断上限与标记收进 `TRUNCATION_MARKER` / `ERROR_TEXT_LIMIT` / `ERROR_TEXT_SHORT_LIMIT`
  / `truncate()`，`gateway_store` / `gateway_runtime` / `dify_workspaces` 引用常量。
- ADR-0212；新增 `tests/common/test_redaction.py`。

## 当前分支或提交

基线 `9b24aad7`。

## 验证结果

- `tests/common tests/gateway tests/dify`：1410 passed
- `ruff check` / `ruff format --check`：通过

## 已知缺口

- **未部署**；线上历史持久化数据不订正。
- `preserve`（订单号）策略未动 —— 需产品确认后再谈。
