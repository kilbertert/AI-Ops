# 租户文案包 QA 计划

契约见同目录 [acceptance.feature](acceptance.feature)，取舍见
[ADR-0186](../../adr/0186-consumer-copy-is-a-published-bundle.md)。

## 自动化用例（本片已实现并跑过）

| ID | 环境 | 前置 | 数据 | 动作 | 预期可观测结果 | 清理 |
|---|---|---|---|---|---|---|
| CB-01 | pytest | 干净库 | `{"languages":{"zh":{"status_lede":"这单已经「{status}」了。"}}}` | `validate` | 通过，返回值与输入一致 | 无 |
| CB-02 | pytest | 同上 | 丢 `{status}` 的 `status_lede` | `validate` | 抛 `CopyBundleInvalid`，错误文本含"占位符"与缺的键名 | 无 |
| CB-03 | pytest | 同上 | 多一个 `{nope}` 占位符 | `validate` | 同上 | 无 |
| CB-04 | pytest | 同上 | 不存在的键 `status_ledee` | `validate` | 错误文本含"没有这个文案键" | 无 |
| CB-05 | pytest | 同上 | 空串 / 401 字 | `validate` | 分别报"不能为空"／超长 | 无 |
| CB-06 | pytest | 同上 | `zh-Hant` 覆盖 | `validate` | 报"整段模板，暂时不能改文案"，并列出可改语种 | 无 |
| CB-07 | pytest | 已发布 zh 覆盖 | 同一份包再发布一次 | `publish_consumer_copy` | 版本号不前进 | 临时库 |
| CB-08 | pytest | 已发布 v1、v2 | `resolve(version=1)` | 取词表 | 得到 v1 措辞；`version=None/0` 得到内置 | 临时库 |
| CB-09 | pytest | 已发布 zh 覆盖 | 真实快照 | `billing_answer(..., pack=...)` | 措辞按租户；金额/来源/免责句逐字不变 | 无 |
| CB-10 | pytest + 网关 | chatflow 桩 | 发布 v1 → 提问 → 发布 v2 → 回读旧 qa | 回读 | 200，正文仍是 v1 措辞（不是 503） | 临时库 |
| CB-11 | pytest + 网关 | 同上 | 把库里正文改成"改过的正文" | 回读 | 拒绝，不返回改动后的正文 | 临时库 |
| CB-12 | CLI | 已存在的 gateway.db | 合法包 / 丢占位符的包 | `aiops-admin consumer-copy publish` | 前者输出 version，后者 `{"published": false, "reason": ...}` 且退出码 1 | 临时库 |

## 真实环境验收（本轮不做，不得记作通过）

- 在 41 上用真实租户发布一份文案包，再用真实会话确认答复采用新措辞、金额与来源不变。
- 需要一次正常的运营发布动作（写库），属于变更而非只读核对；本轮没有做。
- 真机／原 H5 上的观感（换行、`·` 与编号对齐）仍需人眼看。
