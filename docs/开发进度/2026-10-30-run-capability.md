# 「这次跑的是什么」由运行类型说（#1188）

分支 `refactor/run-capability`，基线 `96a06f6f`。

## 落地时发现 ADR-0174 写错了两处

初版 ADR-0174 说「增加 `capability` 列，`order_no` 改为可空」。真正动手时两条证据推翻了它：

**① `operation` 已经是运行类型，再加一列就是同一事实的第二份真源。** 它已经被
`gateway_api` 当分支条件用、被 `operator_cpms` 做两处成员校验，签发时也是按它二选一。
新增一个与它平行的列，只会让「哪一个是判据」重新变成问题——而这正是本票要消灭的东西。

**② `order_no` 不改为可空。** SQLite 放松 `NOT NULL` 要重建整张表，而这是现役运行的记录。
更要紧的是仓库里已经对**同一个形状**做过这个判断：标准诊断表的 `order_no` 同样绑空串、
用 `kind` 区分类别，理由写在 `gateway_store.start_analysis_job` 里——「重建会把每一行既有
诊断记录置于风险中，换来的只是一个『看起来更对』」。那张分析作业单就是活证据。

所以改后的做法比初版**更小**：不增列、不改 schema、没有迁移、没有回填。

## 已完成

- 新增谓词 `diagnoses_an_order(run)`：判据是 `run.operation`，不再看订单号是否为空。
- 九处判断全部换到它上面：`refresh_scope` 的 `allow_empty`、`authorize_readback` 的早返回、
  `_current` 与 `result` 各一份的清单选择、`publication` 的领取判定、`complete` 的协议选择与
  快照、`_worker` 的派发提示、`result` 的渲染。
- 在 `agent_runs` 建表语句旁写下**为什么不改可空**，并指向 `gateway_store` 的同一条判断——
  下一个想改成 `NULLABLE` 的人会先读到这里。
- 新增一条专杀变异的测试：运行类型是无订单回合但订单号非空、以及反过来（订单诊断但订单号为空），
  两行都必须变红。把岗哨写回 `if run.order_no` 时这两行立刻失败。

订单号仍然承担它真正的工作：拼路径、把订单的一个站点冻进授权、比证据。它只是不再兼职
表达「这次跑的是什么」。

## 验证

全量 `uv run pytest`：**3436 passed, 9 skipped**（比上一片多 4 条，来自新的参数化用例）。
`ruff check` / `ruff format --check` / `compileall` 通过。镜像上下文摘要随 `src/` 变更同步，
`context_digest.py --check` match。

## 缺口与边界

- **未完成业务验收**：没有真实 CPMS、没有真实账号、没有真实页面。本片是判据收敛，
  验收判据是「现有断言不改仍全绿 + 变异被杀死」。
- 场景名（`operator_order_diagnosis` 同时被当 scenario 与 operation）**没有改**。
  改名会同时动绑定键与路由判据，是独立的一次决定。
- 本片不新增任何业务能力：站点仍然只在对话回合里可查。

## 下一步

#1189：把授权的载荷从订单号换成「这一次要发的请求」，让新增 CPMS 只读路径不必改 Java。
那张票是本次四张里唯一需要节点配套发版的。
