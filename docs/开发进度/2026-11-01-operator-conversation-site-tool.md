# 管家会话回合站点工具修复

日期：2026-11-01。分支 `fix/operator-conversation-site-note`，基线 `a54a3010`。

## 一句话

`operator_conversation.manifest()` 原来按模板过滤工具，生产模板没有站点工具 ⇒ 会话回合
`tools=[]`，而提示词仍要求模型调它 ⇒ 站点问题必失败。改为**按声明补齐**，并补了一条用
生产形状模板的回归测试。

## 提交

- `src/aiops_diagnostics/operator/operator_conversation.py`（补齐逻辑 + 一句歧义提示词）
- `tests/operations/test_operator_conversation.py`（2 例回归，含变异验证）
- `deploy/m2-live-03/images.lock.json`（`source_sha256` 随源码变更）
- `docs/validation/2026-11-01-operator-conversation-site-tool.md`（证据全文）

## 为什么旧测试没抓到

`tests/operations/test_operator_cpms.py` 的 `environment()` 用 `operator_tools.names()`
造模板，三个工具都在。**夹具与判据同源，夹具就成了唯一事实来源**——这正是
`read-code-before-asserting-root-cause` 那条记的反面教材。

新测试改用**生产形状**的模板（只有两个订单工具），并在删掉修复代码后确认它会红。

## 验证

- 全量 `uv run pytest -n 4`：100%，零 FAILED
- `ruff check` / `ruff format --check`：通过
- `context_digest.py --check`：`match: true`

## 未做

没有部署、没有改 Dify、没有动生产模板。`built_at` 保持原值——没有真的构建镜像。
