# 186. C 端答复的措辞做成按租户发布的文案包，数字仍由代码给

日期：2026-10-30（编号 0186：原稿占用 0185，而 #1210 已先合并
`0185-consumer-tools-authorize-in-the-gateway.md`，按规则改用下一个空闲编号）
状态：已采纳

## 背景

产品的诉求是「想用配置/提示词调整智能体的说话方式」。查过之后，这个诉求在 C 端**落不到
提示词上**：

- `deploy/dify-chat/consumer-service.dsl.yml` 的 `answer` 节点（"选择答复类型与知识引用"）
  系统提示词第一句就是「仅输出 JSON，恰好两个字段 `kind` 和 `faq_id`」。它输出的是**决策**。
- 正文由网关决定：`consumer_knowledge.final_answer` 在 `context["live_snapshot"]` 存在时
  **直接丢掉模型正文**，改用 `live_answer(snapshot, ...)` 渲染；`kind=knowledge` 时用
  FAQ 目录里的 `row["answer"]`；其余 kind 用固定文案表。

所以那条链上模型一个字都不写，改提示词只能改"选哪条路"。同时 FAQ 文案也是**打包在代码里**
（`conversation/faq_catalog.json`，`FAQCatalog.bundled()`），改它同样是一次发版。

## 决策

**把措辞从代码里搬出来，做成按租户发布、按版本钉住的内容；数字与占位符不进这一层。**

- 存储：`consumer_copy_bundles(tenant_id, version, payload_json, published_at, published_by)`，
  主键 `(tenant_id, version)`，**只增不改**。当前版本 = 该租户最大版本号。
- 形状：`{"languages": {lang: {key: text}}}`，只能覆盖**内置文案已有的键**。
- 发布校验（`consumer_copy.validate`）：语种受支持、键存在、非空、≤400 字、
  **占位符集合必须与内置文案逐字相同**。少一个 `{order_no}` 就是把订单号从答复里删掉，
  那种"改措辞"破坏的是可核对性，必须拒绝而不是容忍。
- 渲染：`consumer_plain` 的两个拼装函数与 `status_answer`／`billing_answer`／
  `live_answer` 接受 `pack`；不传就是内置文案（行为与引入前逐字相同）。
- **版本钉住**：答复生成时把 `copy_version` 写进 run context（与既有
  `answer_language_version` 同一位置），回读按那一版渲染。运营发布新文案不会让在途答复
  对不上，也不会把"改措辞"变成 503。
- 运维入口：`aiops-admin consumer-copy publish|show`。

## 为什么不是"让模型写正文"

那是最省事的一条路，但它把「数字」和「措辞」绑在一起交给模型：今天由代码保证
`27.36` 来自本次取证到的快照（ADR-0083），改完之后这个保证就没有了，而且每次答复多一次
模型调用、多一类失败模式。文案包保住的分工是：**数字由代码给，语气由人定。**

## 被否掉的替代形态

- **只改 Dify 提示词。** 上面对过了：那条链上模型不写正文，改提示词改不到措辞。
- **让运营改 FAQ 目录。** FAQ 是打包在代码里的 JSON，改它一样是发版；而且 FAQ 只覆盖
  `kind=knowledge` 那一支，订单状态/账单走不到。
- **把整段模板也允许覆盖（九个本地化语种）。** 那九种的正文是
  `consumer_language.json` 里的**整段模板**（`status`/`billing` 一条几百字，含占位符与
  免责句），用 400 字上限去管它只会逼人写错，而放宽上限等于允许一次发布改掉整段结构。
  **明确拒绝**并说明原因，而不是"发布了但悄悄不生效"。
- **按"当前最新版"渲染而不是按钉住的版本。** 运营发布一次文案就会让在途答复对不上，
  回读变 503 —— 把改措辞变成掉线。
- **发布时允许新增键。** 新增键说明发布者以为自己在改一个不存在的地方；静默忽略会让
  人以为改动生效了。

## 代价

- **只覆盖 `zh`／`en`。** 这两者的正文是逐句组合的（`consumer_plain` 的小键词表），
  "改一句"能精确对应一个键。其余九种（含 `zh-Hant`）仍是整段模板，要迁成组合式才能改，
  那是独立一片活；`validate` 现在会明确拒绝并说明原因。
- 文案包只作用于 **chatflow 路线的 live 快照渲染**。节点路线（`answer_from_run`）仍用内置
  文案，因为它的回读比对没有 context 可钉版本。这是本片的已知缺口，不是遗漏。
- 每次答复多一次 SQLite 读（取当前版本）与一次回读时的按版本读；都是主键查询。
- 措辞改动**不受**代码评审覆盖：发布是运维动作，靠校验与台账（版本、时间、发布者）兜。
  文案写错字不会被 CI 拦住，只会被读的人发现。

## 验收

契约见 [acceptance.feature](../acceptance/consumer-copy-bundle/acceptance.feature)，
执行步骤见 [qa-plan](../acceptance/consumer-copy-bundle/qa-plan.md)。
`tests/consumer/test_consumer_copy.py` 19 条，覆盖：占位符/未知键/空/超长/不可改语种的拒绝、
合并只影响被覆盖的语种、版本只增不改且相同内容不推进、租户措辞生效而数字不动、
按钉住版本复现、**端到端「发布新文案后在途答复回读仍是旧措辞」**、篡改仍被拒。

**变异验证**：把 `validate` 的占位符检查去掉，`test_invalid_bundles_are_rejected_with_the_reason`
与 `test_placeholder_check_covers_every_language` 变红；把 `resolve` 改成"取最新版"，
`test_resolve_follows_the_pinned_version_not_the_latest` 与端到端回读那条变红。

未覆盖：真实租户发布与真机观感；本片**未完成业务验收**。

## 审查后补的两条约束（2026-10-30，Codex 评审）

初版把这两点做漏了，两处都会造成真实故障，记在这里因为它们不是实现细节：

- **非组合式语种必须返回"无覆盖"而不是一份词表。** `bundled()` 对未收录语种退回英文，
  所以若 `resolve` 对 de/fr/zh-Hant/th 也返回一个 pack，一个**只发布了中文文案**的租户
  会让德语答复的来源标签与整段模板变成英文。`resolve` 因此对
  `language not in COMPOSED_LANGUAGES` 返回 `None`，含义是"按既有本地化路渲染，一行都不改"。
- **格式写法要在发布时验，不能留到渲染时。** `Formatter().parse` 只返回字段名，
  `{status:{missing}}`、`{status!z}`、`{status:invalid}` 都能通过占位符比对，
  却在渲染时抛 `KeyError`/`ValueError` —— 那是把"发布时能发现"变成"用户提问时 500"。
  发布校验因此用假值真跑一遍 `format()`，异常即拒。
