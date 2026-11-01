# 管家会话回合不下发站点工具：修复与回归测试

日期：2026-11-01。分支 `fix/operator-conversation-site-note`，基线 `a54a3010`。

## 问题

2026-11-01 的 D 项双端复验（[证据](2026-11-01-t13-d-dual-entry-reverify.md)）发现：管家端
站点类问题 **4/4 失败**，服务端 `OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE`。

## 根因

`operator_conversation.manifest()` 按**模板**过滤工具：

```python
for tool in body["tools"]:  # 模板 = target.manifest_file
    if "conversation" not in declared.applies_to:
        continue
    kept.append(...)
```

生产模板 `operator-exec-manifest.json` 只有 `cpms_order_detail` / `cpms_charging_samples`
（都只属 `order`），于是 `kept=[]`。**但同一份 INSTRUCTIONS 又要求模型
「先调用 `cpms_site_search`」**——模型只能写字，网关按
`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收。

为什么测试一直是绿的：`tests/operations/test_operator_cpms.py` 的 `environment()` 用
`operator_tools.names()` 造模板，三个工具都在。**夹具与判据同源，夹具就成了唯一事实来源。**

而设计意图（#1205 / ADR-0182）明确是「无订单回合可以带工具，但只能是声明里允许这类运行用的
那些」——判据是 `applies_to`，不是模板。

## 修复

`operator_conversation.manifest()` 改为：**模板里有且属 conversation 的保留；模板没列的、
但声明里属 conversation 的，按声明补齐。**

```python
kept, seen = [], set()
for tool in body["tools"]:  # 模板优先
    ...
    kept.append({**tool, "input_schema": declared.input_schema()})
    seen.add(tool["name"])
for declared in operator_tools.tools():  # 声明补齐
    if "conversation" not in declared.applies_to or declared.name in seen:
        continue
    kept.append({"name": ..., "description": ..., "input_schema": declared.input_schema()})
```

这样生产模板是否列了站点工具都不再影响会话回合——这正是 ADR-0182 说的那条判据。

顺带把 INSTRUCTIONS 里那句有歧义的「没有检索工具」改成明确的
「没有知识库检索工具，不得声称读过知识库或业务数据；站点查询只走 cpms_site_search」。
原句与「先调用 `cpms_site_search`」并排，读起来像"没有任何工具可用"。

## 回归测试

新增两例（`tests/operations/test_operator_conversation.py`）：

| 测试 | 钉住什么 |
| --- | --- |
| `test_conversation_tools_come_from_declaration_not_from_template` | 用**生产形状**的模板（只有两个订单工具），会话回合仍必须下发 `cpms_site_search`，且不下发订单工具 |
| `test_template_site_tool_is_not_duplicated` | 模板已列该工具时不重复；描述沿用模板，schema 仍以声明为准 |

**变异验证**：删掉补齐那段后，第一条立刻变红（`1 failed, 1 passed`）。恢复后两条全绿。

## 镜像摘要

改 `src/` 会改变 `deploy/m2-live-03/context_digest.py` 算出的内容摘要，因此同步更新了
`images.lock.json` 的 `source_sha256`（只改这一行，`built_at` 保持原值——没有真的构建镜像）。
`context_digest.py --check` → `match: true`。

## 验证

- `uv run pytest -n 4` 全量：100%，零 FAILED
- `uv run ruff check .` / `ruff format --check .`：通过

## 未做

没有部署到 41、没有改 Dify、没有动生产模板。部署时按 CD 流程走；本 PR 只保证
「模板缺项时会话回合仍拿到站点工具」这条不变量成立。
