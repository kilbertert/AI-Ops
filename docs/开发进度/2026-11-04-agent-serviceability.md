# 2026-11-04：智能体可服务性判据收成一处（#922）

## 目标

`status == "published"` 与「草稿编辑不改变正在运行的已发布版本」不能同时成立。
把「还有没有版本在服务」收成一个纯函数，两处调用点改为委托它。

## 已完成内容

- 新增 `src/aiops_diagnostics/publication/agent_serviceability.py`
  （`live_version` / `serves` / `LIVE_STATUSES`），ADR-0209。
- `qa_rag.select_customer_agent` 与 `gateway_runtime._is_active` 改为调用它。
- 新增 3 条测试；对两处调用点各做一次「改回旧判据即红」的反向验证。

## 当前分支或提交

`fix/agent-serviceability`，基线 `9b24aad7`。

## 验证结果

- `tests/publication/ tests/gateway/`：300 passed
- `ruff check` / `ruff format --check`：通过

## 已知缺口

- **未部署**；未在 41 上做真实编辑窗口验收。
- #922 正文里的两条判据（`promo_agents` / `ops_agents`）已在 #1125 随 Python harness
  退役删除，本轮只收敛剩下的两处。
