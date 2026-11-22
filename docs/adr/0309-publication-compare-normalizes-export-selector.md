# 309. 发布中央比对复用候选摘要归一化，不改 canonical 内容形态

状态：已采纳；修复比对口径缺陷，不改 DSL 规范化函数的对外语义。

`environment_publication.central_bundle` 用逐字 dict 比对「`export_dsl` 导出的工作流内容」与
「`environment_consumer.document()` 重建的期望文档」。两个 Dify 端点表示不同：草稿详情省略
环境变量的 `selector`（`document()` 也明确 pop 它），而导出端点给每个变量补回
`selector: ["env", name]`。逐字比对因此恒为假，发布任务在 central 步确定性
`ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED`（env95 第二租户实测，宿主复现证明唯一
diff 即 selector 字段）。

## 决定

比对两侧统一改走 `environment_consumer_trial.candidate_workflow_sha`：它先对
`workflow_content` 结果剥掉 `selector == ["env", name]` 这一项已知表示差异，再取
`workflow_of` 稳定摘要。该函数正是 `describe()`（发布 spec 的 `workflow_sha256`）与
候选项试运行链已在用的规范化口径，本次不引入第二种「什么叫同一份工作流」的算法。
导出的 `workflow` 本体不做任何 strip：它会被冻结进 `intent`/`release.json`，并在发布回读
（`export_dsl(workflow_id=...)`，双侧均含 selector）处继续逐字比对，冻结体必须保持导出原样。
`central_bundle` 仍自包含重算 `document()` 与导出内容，不新增对 spec 字段的依赖；
`selector` 为非 `["env", name]` 形状时比对仍失败——未知的表示差异按设计拒绝而不是静默吞掉。

## 替代方案

在 `workflow_content` 内全局剥 selector：一处修复所有调用点，但 `workflow_content` 的输出
同时是冻结快照与去重比对的 canonical 形态（`dify_dsl_pull` 同 app 历史版本比对、
`self_service` 的 graph_sha 契约）。改变 canonical 形态会让历史冻结体与重算体在 selector
字段上系统性不等，把一次性比对缺陷变成跨账本形态迁移——明确排除。
就地剥 `workflow` 本身的 selector：会破坏 `intent`/`release.json` 冻结体与发布回读
（双侧导出、均含 selector）的逐字一致性，同样排除。
只对导出侧归一化而不对期望侧：期望侧经 `document()` 已无 selector，效果等价，但双侧
同口径调用让「归一化只忽略已知差异」这一断言在两侧都成立，不依赖期望侧永远无 selector。
`spec["consumer"]["workflow_sha256"]` 直接复用：`describe()` 在同一 dispatch 内已重算并校验
spec 相等，值可信任，但 central_bundle 自包含重算使该函数不新增对 spec 内部字段的依赖，
选用本决定形状。

## 后果

central 步的「发布源未被改动」判据恢复设计语义：业务内容任何真实差异仍逐字拒绝，
`["env", name]` selector 这项 Dify 导出噪声不再计入。回归测试把 `export_dsl` 替身注入
同款 selector 并断言冻结 workflow 保留它——归一化只发生在比对一侧的事实被长期锁死。
若 Dify 未来改变 selector 形状（非 `["env", name]`），比对按未知差异拒绝，
届时需显式扩大已知差异清单而不是静默放行。
