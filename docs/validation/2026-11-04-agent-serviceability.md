# 2026-11-04：智能体可服务性判据收成一处（#922）

**里程碑目标**：把「这个智能体现在还有没有版本在服务」从四处各写一遍、两个方向相反
的答案，收成 `publication/agent_serviceability.py` 的唯一判据。

## 已完成内容

- 新模块 `agent_serviceability.py`：`live_version(agent)` / `serves(agent, version_no)`，
  只含纯函数与冻结值。
- `qa_rag.select_customer_agent`：`status != "published" or published_version is None`
  → `live_version(agent) is None`。
- `gateway_runtime._is_active`（媒体活性）：`published.status != "published"`
  → `live_version(published) is None`。
- `AgentStore.disable()` 的 UPDATE 由 `status = 'published'` 放宽为
  `status IN ('published', 'draft')`。判据改成读指针后，草稿仍在服务，这条 UPDATE
  是唯一的下线通道；不放开它，草稿态**无法下线**（Codex Review 指出，P1）。

**行为变化（显式列出）**：

1. `draft` + 非空 `published_version`：客服问答继续选它、已签发媒体继续可取。改前两者都断。
2. `disabled`：仍然立即停服（两个方向都不变）。
3. 从未发布（`published_version IS NULL`）：仍然不服务。

## 已核实失效的旧 claim

改动前策来源（`promo_agents.py`、`ops_agents.py`）在 #1125 已随 Python harness 退役
删除，因此 #922 正文里「停用后宣传路径仍在服务」的那两条判据已无代码。本轮的收敛对象
是**剩下的两处**（`qa_rag`、`gateway_runtime`），数量从四降到二。

## 验证结果

| 检查 | 结果 |
|---|---|
| `uv run pytest tests/publication/ tests/gateway/` | 300 passed |
| `uv run pytest tests/gateway/test_media_api.py tests/publication/` | 114 passed |
| `ruff check` / `ruff format --check` | 通过 |

**新测试**：

- `tests/publication/test_agent_serviceability.py`：判据表逐行（`published`/`draft` ×
  有指针/无指针、`disabled`），`serves` 的相等语义。
- `tests/publication/test_qa_rag.py::test_a_draft_edit_does_not_take_the_live_version_offline`
  ——真实 `AgentStore`/`AgentManager`，`fork_draft` 后选择器仍选中 v1。
- `tests/gateway/test_media_api.py::test_a_draft_edit_does_not_403_media_it_already_produced`
  ——真实 runtime，`fork_draft` 后 v1 签发的媒体仍 200。
- `tests/publication/test_agent_serviceability.py::test_a_draft_agent_can_still_be_disabled`
  ——真实 store，`fork_draft` 后仍能 `disable`，且 `live_version` 变 `None`。

**反向验证（逐条实测）**：

- 把 `qa_rag` 改回 `status != "published"` → 新问答测试红。
- 把 `_is_active` 改回 `status != "published"` → 新媒体测试红（既有媒体测试仍是绿的，
  因为它们的草稿窗口都被「随后立即重新发布」掩盖 —— 这正是缺口）。
- 把 `disable()` 的 WHERE 改回 `status = 'published'` → 草稿下线测试红。

**真实故障业务验收：未完成** —— 本轮未在 41 上跑真实编辑窗口；本地证据只证明判据与
两个调用点一致。

## 已知缺口

- **未部署**。
- 媒体侧刻意不使用 `serves`（保留 ADR-0046 的「更旧版本活在 TTL 内」），因此「草稿窗口
  内**重新发布**到新版本」的行为仍由 `_is_active` 的 KB 绑定检查决定，不在本判据内。
- `_pinned_promo_agents` 的 `startswith("agt_")` 弱解析仍是 #799 Further Notes 记的候选，
  本轮不碰。
