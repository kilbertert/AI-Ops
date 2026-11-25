# ADR-0346：退役已归档的早期联调大文档、测试专用多语言导出与同名金额解析器歧义

日期：2026-11-25
状态：已采纳

## 背景

在完成 [ADR-0343](0343-prune-redundant-drafts-and-obsolete-archives.md)、[ADR-0344](0344-remove-dead-symbols-and-split-gateway-bridge-srp.md) 与 [ADR-0345](0345-offload-legacy-receipts-to-archive-repo.md) 之后，全仓仍遗留四项经人工审查并确认取舍的结构性债务：

1. **四份早期历史联调大文档及其测试锚定**：
   - `docs/agents/frontend-api-brief.md`（约 68 KB，已被 [public-openapi.md](../agents/public-openapi.md) 替代）
   - `docs/agents/operator-repair-blueprint.md`（约 26 KB，已被 [operator-cpms/README.md](../agents/operator-cpms/README.md) 与现役交付索引替代）
   - `docs/agents/p0-media-canary.md`（约 25 KB，历史媒体灰度检查单）
   - `docs/agents/testing-api-reference.md`（约 16 KB，早期内部接口速查，与 OpenAPI 重复）
   上述 4 份文档已连同历史回执一并完整归档至私有归档仓 `kilbertert/AI-Ops-history-archive@759e45e85b9731a4b44ac85c5d1a491d829f498e`，仅因 `tests/gateway/test_assistant_cancel_handoff.py` 与 `tests/integrations/test_l4_order_authorization.py` 存在静态文件引用而留在主仓。
2. **仅由测试引用的多语言兼容导出**：
   `src/aiops_diagnostics/common/i18n.py` 中的 `ZERO_ORDER_REMINDER_MESSAGES`、`zero_order_reminder`、`DIAGNOSIS_FAILURE_MESSAGES`、`diagnosis_failure_message` 在全部现役生产代码中已无任何调用方，仅由 `tools/content/derive_zh_hant.py` 与 `tests/common/test_i18n*.py` 锚定。
3. **同名 `_amount()` 金额解析函数歧义与孤立死代码**：
   仓库中原有四处同名私有函数 `_amount()`，各自承担截然不同的语义：
   - `consumer/billing.py`：严格 `Decimal` 解析，遇非法/负值/过大值抛出 `ChargingStatusError`；
   - `consumer/presentation.py`：保留原样非空展示字符串，不做数值转换；
   - `operator/order_billing.py`：从字段字典提取非负 `Decimal`，无效值返回 `None`；
   - `consumer/consumer_facts.py`：经静态与调用面核查，全仓零调用方（连同其独占的 `Decimal`/`InvalidOperation` 导入均为死代码）。
4. **子包内模块前缀命名风格（历史遗留）**：
   既有子包内存在 `consumer/consumer_facts.py`、`gateway/gateway_api.py`、`operator/operator_*` 等携带包名前缀的文件名，在全仓审查时需明确是否执行全量重命名。

## 决策

1. **退役四份历史大文档与对应旧契约测试锚定**：
   - 从主仓移除上述 4 份已归档历史大文档（原文保留于 `kilbertert/AI-Ops-history-archive@759e45e8`）；
   - 同步移除 `tests/gateway/test_assistant_cancel_handoff.py` 中对 `docs/agents/p0-media-canary.md` 的旧归因断言，以及 `tests/integrations/test_l4_order_authorization.py` 中对 `docs/agents/operator-repair-blueprint.md` 的计数断言（保留对现役 `docs/agents/current-delivery-state.md` 的一致性校验）；
   - 将现役文档与注释中的引用改指向 [public-openapi.md](../agents/public-openapi.md) 或归档仓坐标（`docs/agents/env-41-runbook.md` 因被并行发布分支修改而暂不触碰，待该分支合入后再顺带清理其纯文本提及）。
2. **移除测试专用的 `i18n` 历史导出**：
   - 从 `src/aiops_diagnostics/common/i18n.py` 移除 `ZERO_ORDER_REMINDER_MESSAGES`、`zero_order_reminder`、`DIAGNOSIS_FAILURE_MESSAGES`、`diagnosis_failure_message`；
   - 同步精简 `tools/content/derive_zh_hant.py` 与 `tests/common/test_i18n*.py`，不再为无生产调用方的历史导出维持繁简衍生表与单测。
3. **对金额解析器做具名区分并移除死代码**：
   - **不强行合并**语义不同的金额解析逻辑，按各自真实契约重命名：
     - `consumer/billing.py`：`_amount` → `_strict_decimal_amount`（同步更新 `tests/consumer/test_consumer_billing.py`）；
     - `consumer/presentation.py`：`_amount` → `_raw_amount_text`；
     - `operator/order_billing.py`：`_amount` → `_non_negative_decimal`；
   - 直接删除 `consumer/consumer_facts.py` 中全仓零调用的 `_amount` 及其孤立导入。
4. **子包既有模块文件名保持现状，新模块执行去冗余前缀规则**：
   - 不对既有 ~40 个带包名前缀的子包模块执行全量重命名；
   - 后续新增子包模块时默认不重复外层包名（例如已在 `consumer/` 下则优先使用 `facts.py` 而非 `consumer_facts.py`），在重构特定子包时再按需自然演进。

## 备选方案

- **备选方案 A：把所有 `_amount()` 合并为 `common/` 下的单一公共金额解析函数**
  - 放弃原因：三处调用方对空值、非数字、负数、字符串保真和异常语义的要求互不兼容；强行合并需要堆叠布尔开关参数，反而增加调用方误用风险与认知成本。
- **备选方案 B：一次性全量重命名所有带子包前缀的 Python 模块文件**
  - 放弃原因：将牵动 40+ 模块、数百处 `import` 路径、现有测试、历史 ADR 锚点及并行开发分支，制造极高的合并冲突面与回溯成本，且不产生任何运行时或可维护性收益。
- **备选方案 C：保留四份历史大文档与 `i18n` 导出，仅在文首加过期横幅**
  - 放弃原因：主仓依然存在约 135 KB 与现行 OpenAPI / 交付索引冲突的过时文档及 139 行零生产调用方的多语言代码，持续误导新工程师与 AI 检索；完整内容已在独立私有归档仓受 SHA-256 校验保存，无需在主开发树双份保留。

## 影响与验证

- 主仓文档消除 4 份过时联调大文档与重复的测试锚定，全仓现役文档保持 0 断链；
- `common/i18n.py` 删减 139 行死代码，繁简衍生脚本与多语言测试仅覆盖现役生产导出；
- 三处现役金额解析器各自具备自解释名称，`consumer_facts.py` 移除死代码；
- 全量 `ruff`、`pytest`、包构建与 `adr.check.mjs` 均需通过；不触碰现网运行行为边界（仍严格只读诊断）。
