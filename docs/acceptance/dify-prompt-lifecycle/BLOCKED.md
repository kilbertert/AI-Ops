# 待完成项

## 已完成（减少本文件条目）

- **受管 UI 页面闭环：117/117 通过**（2026-10-31，基线 `5f1ebe2c`）。
  用自己从 `1.17.1` 构建的 `aiops/dify-web-patched:prompt-managed-ui-5f1ebe2c`，
  在隔离栈里真实操作「编辑 SYSTEM → 预览 → 发布 → 恢复」，草稿不污染正式版，
  文档流与 10 种语言 CPMS 全通过，清理无残留。
  原件 `docs/validation/2026-10-31-prompt-managed-ui-acceptance.md`。
  **注意边界**：该轮模型是合成的，证明的是页面与版本接线，不是模型表达质量。
- 代码已合并并上线 41（`05e82e8e` → `5f1ebe2c` → `4ec55c7a`），技术链路与真实用户端到端已验。
- **固定 32 次真实模型样本：已执行，最新一轮 31/32。**
  唯一失败是真实模型错误（状态快照里没有账单事实，模型却写了 `{{fact:billing_line}}`），守卫按设计拦下。
  该批次还抓到一个守卫假阳性（A-不-A 疑问式、`充电站` 类名词被当成状态改写），已修复并上线（`4ec55c7a`），
  带 4 条正例 + 4 条负例回归。原件 `docs/validation/2026-10-31-model-samples-32-result.md`。
  **注意边界**：同一执行器换轮跑失败项会变（30/32、30/32、31/32），单轮数字不代表稳定性。

## 仍未完成

- **生产原应用启用事实片段契约（`slots`）：未执行，是有意停下的。**
  只读核对确认生产应用 `394b5399-…` 的环境变量只有 `cpms_status_enabled` /
  `cpms_billing_enabled` / `knowledge_answer_mode=documents`，**没有 `answer_fact_contract`**，
  所以线上仍走 `legacy`；SYSTEM 提示词 1338 字符且 `{{fact:` 出现 **0** 次、无协议块。

  **为什么不能只加环境变量**：`render_segments` 要求必需的证据片段，提示词不产出
  `{{fact:…}}` 时每个订单/账单问题都会整轮失败。两件事必须同一次发布完成。

  **为什么没有直接跑仓库脚本**：唯一入口 `tools/content/upgrade_consumer_flow.py` 会
  `candidate["workflow"]["graph"] = template["graph"]` —— **整张图换成仓库模板**后再拷回部分字段，
  等于重写整个生产 chatflow（含 SYSTEM、节点结构、memory）。这超出"启用契约"的范围，
  不应在没有明确同意的情况下执行。

  已就绪：草稿回滚点 `sha256 ff5c5d33…`（20,632 字节）；已发布版本 `2026-10-30 10:07:00`；
  只读读取器 `tools/verification/inspect_production_prompt.py`。

- **41 原应用 / 原 H5 的真实模型页面验收：未执行。** 32 次样本直接调模型出口+校验器，
  不过 Dify 页面；117 项页面验收用的是合成模型。两者都不能替代生产原应用的真实链路。
- **线上 `max_tokens` 未知**：`exec-manifest.json` 的 `model_options` 是 `{}`，仓库内无法核实实际生效值。
  该模型推理峰值观测到 3194；若上游默认低于此值会出现"正文为空→整轮失败"。
  生产 12 条失败因 `error_message` 为空无法归因，未下结论。需另开任务抓真实请求确认。

未完成业务验收的完整口径，不以单测、隔离栈或模型样本代替生产原应用上的页面交付。
