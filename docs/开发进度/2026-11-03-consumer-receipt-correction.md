# 消费者回执订正（#1316 复核闭环）

日期：2026-11-03　分支：`docs/consumer-receipt-correction`　PR：#1316

## 里程碑目标

把 #1316 四条未解决讨论逐条落到记录里，并把本轮新发现的归因错误一并改正。
纯文档订正，不改代码、不动生产。

## 已完成内容

1. **越权样本的版本归属**：明确它不是"v17 的一次运行"。请求在
   `_require_visible_order`（`gateway_api.py:6888`）就 404 `ORDER_NOT_FOUND`，
   `assistant_questions` 无新行，没有任何 workflow 被调用。已在
   [验收结果](../validation/2026-11-02-consumer-three-acceptance-result.md)、
   [收尾记录](../开发进度/2026-11-02-consumer-acceptance3-closeout.md) 和状态索引三处
   改成"无版本 / 不属于任何 workflow"，并从 v18 证据表里移出（原先放在"改措辞之前"一栏，
   仍暗示它属于某个版本）。
2. **恢复步骤拆成三种动作**：改草稿 / 发布草稿 / 恢复历史正式版本，各自写清生效范围与
   回读验证，并明确指出「保存 ≠ 恢复」。原写法"控制台改回旧文案即可"会让运营以为
   线上已回退，实际正式入口仍读冻结快照。
3. **业务 SYSTEM 重置登记为新基线**：139 字符等于 `consumer_documents.PROMPT`
   （已在本机核对逐字相同），旧文案未入库，私有留档位置写明。
4. **配置优先级写进索引**：`_env(...) or _file_value(...)` 的顺序——
   进程环境优先、缺省回落 `production.env`。这解释了为什么"只在 production.env"不能
   推论成"读不到"，也解释了为什么追加 `gateway.env` 不是那次成功的成因。

## 当前分支或提交

`docs/consumer-receipt-correction`，基线 `7116edd0`（origin/main）。

## 验证结果

- `git diff --check`：无空白错误。
- 文档与源码对照逐条复核，见 [验证记录](../validation/2026-11-03-consumer-receipt-correction.md)。
- 本 PR 不含代码变更，因此没有可跑的行为测试；确定性 CI 只做格式与布局检查。

## 已知缺口

- 原「加了 `gateway.env` 三键并重启后成功」这件事的**真实成因仍未查清**。
  本轮只订正归因、不删键、不重启；查清需要当时的失败 HTTP/配置来源/进程状态证据，
  这些证据本轮没有。**不要**把它当作已解释的问题。
- v18 上仍未补跑知识问答与越权样本，跨租户反例仍未做。
- 账单截图与旧文案留在操作者私有目录，未入库。

## 下一步

- Codex 统一协调合并与生产发布；合并本身不改变任何生产状态。
- 补跑 v18 知识与越权、跨租户反例，按
  [提示词生命周期验收](../acceptance/dify-prompt-lifecycle/acceptance.feature) 的受管流程做，
  不用控制台保存冒充发布。
