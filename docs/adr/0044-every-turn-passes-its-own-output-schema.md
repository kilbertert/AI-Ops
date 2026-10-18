# 44. 每个回合都显式传自己的输出 schema

日期：2026-10-19
状态：已采纳
影响版本：`src/aiops_diagnostics/agent_runner.py`（零阶回答与路由分类器）、
`src/aiops_diagnostics/agent_contracts.py`（`classifier_turn_schema`）

## 背景

`SDKCodexSession.run` 的签名是 `output_schema: dict | None = None`，而它内部是：

```python
handle = self._thread.turn(prompt, output_schema=output_schema or agent_turn_schema())
```

`agent_turn_schema()` 是**诊断**的 schema。于是「不传」不是一个中性的缺省，而是
**默认用诊断合同去要求模型**：严格 provider 会照这个 schema 约束模型输出，
模型给回的就不再是提问者要的形状 —— 提示词里写什么都没有用，因为约束在 schema 上。

生产实测（41，2026-10-19）：一次零阶「你好」回合返回

```json
{"kind":"diagnosis","diagnosis":{"schema_version":"1.0","incident_id":"",...,"summary":"","root_cause":""}}
```

`payload["text"]` 不存在 → `payload.get("text")` 得 `None` → 作业以
`QA_FAILED zero-order answer returned empty text` 结束 → **用户什么也拿不到**。
rollout 日志里模型的推理是"应该用中文友好问候用户"，即它**读懂了任务**，
但被 schema 逼成了诊断形状。

**间歇性**是它活下来的原因：只有 provider 真正执行 schema 时才复现。同一天 7 次实测
复现 1 次；41 历史上这条错误只有 1 行（就是那一次），所以它既不是"必现"，
也从来没被当成"必不现"处理过。

## 决策

**每个回合显式传自己的 `output_schema`；不依赖 `run` 的缺省。**

- 零阶回答传 `qa_turn_schema()`（`{"text","reminder"}`）。这个函数**早就存在**却
  从未被调用过 —— `qa_rag` 明确传了自己的 `qa_rag_turn_schema()`，只有零阶与分类器
  漏了。缺的那根线不是"新功能"，是**接线**。
- 路由分类器传新增的 `classifier_turn_schema()`（`intent`/`confidence`/`risk`/`answer`）。
  它的缺失更隐蔽：模型回诊断形状 → 解析器拿不到 `intent` → 上层读作"没有路由结论"
  → **寒暄处理、宣传意图、高风险追问规则全部静默失效**，而表面没有任何报错。
  这是同一个缺陷在两个调用点上的两种表现。

## 被否掉的替代形态

- **改 `run` 的缺省，让不传就报错。** 更彻底，但 `agent_engine.py:249` 与诊断线
  合法地依赖诊断缺省；在一个修两处漏传的改动里改全局缺省会连带改诊断线行为。
  留作后续：等所有调用点都显式传之后，再把缺省改成"必须显式"。

- **只在提示词里更强调"按 JSON schema 输出"。** 提示词已经写着"只输出 JSON（遵循
  结构化输出 schema）"——模型照做了，只是照的是**被给的那个** schema。
  改提示词治不了这个病。

- **在解析层容忍诊断形状。** 等于让零阶路径接受一个没有正文的载荷，把"故障"
  伪装成"回答"——正是本仓库在别处反复拒绝的那种做法。

## 后果

更容易：一个回合用什么合同，在该回合的调用点上一眼可见，不再需要读
`codex_runtime.run` 的缺省才能推断。新增契约（如 `ops_analysis_narrative`）
照抄这个形状即可。

更难：每加一个回合契约都要同时加 schema 与调用点参数。这是刻意的：
"这个回合的合同是什么"必须是一个被写下来的决定。

上限：本记录只保证**回路**接对了。schema 与提示词、解析器三者是否互相一致
（例如 schema 要求 `reminder` 必填而模型漏给）不在本记录范围内，
由各自的合同测试负责。
