# C 端答复措辞做成按租户发布的文案包

分支`feat/consumer-copy-bundle`，基线`ebc56d5d`。
设计取舍见 [ADR-0185](../adr/0185-consumer-copy-is-a-published-bundle.md)，
契约见 [acceptance.feature](../acceptance/consumer-copy-bundle/acceptance.feature)。

## 目标

产品要"用配置调整智能体的说话方式"。查证后 C 端的正文**不由模型写**（chatflow 的
`answer` 节点只输出 `{kind, faq_id}` 决策，正文由 `final_answer` 用快照/FAQ/固定文案渲染），
所以提示词改不到措辞。本片把**措辞**从代码里搬出来，做成按租户发布、按版本钉住的内容，
同时保住"数字由代码给"。

## 已完成内容

- 新增 `consumer/consumer_copy.py`：文案包形状、发布校验（语种/键/非空/长度/占位符）、
  内置+覆盖的合并、按钉住版本解析。
- `gateway_store.py`：`consumer_copy_bundles` 表（只增不改）+ `publish_consumer_copy`
  （内容相同不推进版本）+ `consumer_copy`（取当前或指定版本）。
- 渲染接线：`consumer_plain.status_text`／`billing_text`、`status_answer`、`billing_answer`、
  `consumer_knowledge.live_answer`／`final_answer` 接受 `pack`；不传即内置文案，行为与引入前逐字相同。
- **版本钉住**：run context 增加 `copy_version`；回读按那一版渲染
  （`consumer_chat_service.copy_pack`）。发布新文案不会让在途答复变 503。
- 运维入口：`aiops-admin consumer-copy publish|show`。
- README 模块表补 `consumer.consumer_copy.py`；内容锁按新树重建。

## 覆盖范围（刻意收窄）

只有 `zh`／`en` 可以改：这两者的正文是逐句组合的，改一句能精确对应一个键。
其余九种（含 `zh-Hant`）仍是 `consumer_language.json` 里的整段模板，`validate` 会
**明确拒绝**并说明原因 —— 不做"发布了但悄悄不生效"。

## 验证

| 项 | 结果 |
| --- | --- |
| `ruff check .` / `ruff format --check .` | 通过（1142 files already formatted） |
| 全量 `pytest -n 4` | **3528 passed / 9 skipped** |
| 本片专项 | 新增 **26** 条（校验拒绝含格式说明符、非组合式语种回归、合并、版本、钉版本复现、端到端回读、篡改仍拒） |
| 变异验证 | 去掉占位符校验 → 2 红；`resolve` 取最新版 → 2 红（含端到端）；去掉非组合式语种防线 → 4 红；去掉格式实跑校验 → 3 红 |
| CLI 手测 | 合法包出版本号；丢占位符的包被拒并指出缺 `['status']`；错别字键被拒 |
| 内容锁 | `build-image.sh --update-lock` 重建 |

## 已知缺口

- **未完成业务验收**：没有在 41 上发布过真实租户文案，也没有真机观感。
- 节点路线（`chat_execution.answer_from_run`）仍用内置文案：它的回读比对没有 context 可钉版本。
- 九种本地化语种暂不可改（需要先迁成组合式渲染）。
- 文案错别字不会被 CI 拦住，只能靠发布校验与读的人发现。
- 审查发现过一条真实回归：`resolve` 曾对非组合式语种返回英文兜底词表，会让只改中文的
  租户把德语答复变成英文。已修并加参数化回归用例。

## 下一步

1. 推分支、开 PR、跑确定性 CI。
2. 上线后在 41 上用真实租户发布一份文案，确认措辞生效且金额/来源不变。
3. 若确需非中文语种可改，另起一片把那九种迁成组合式渲染。
