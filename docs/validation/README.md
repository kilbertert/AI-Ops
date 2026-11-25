# 现役验收回执目录

- **按领域查找当前验收结论与证据**：优先阅读 [当前交付状态索引](../agents/current-delivery-state.md)（按领域/组件列出对应回执，不凭文件名排序判断最新）。
- **直接浏览本目录全部现役回执文件**：[打开 `docs/validation/` 目录树](../validation/)（当前保留 134 份现役及传递引用的 Markdown 回执 + 16 份配套结构化证据，后续新验收事件继续按 `docs/validation/<日期>-<短名>.md` 新增）。
- **历史归档去向**：依据 [ADR-0345](../adr/0345-offload-legacy-receipts-to-archive-repo.md)，早期单体归档 `docs/validation.md` 与 334 份不再被现役文档、保留回执或 ADR 引用的历史按日回执已完整迁出至私有归档仓库 [`kilbertert/AI-Ops-history-archive`](https://github.com/kilbertert/AI-Ops-history-archive/tree/759e45e85b9731a4b44ac85c5d1a491d829f498e)（归档提交 `759e45e8`，含 `MANIFEST.sha256` 校验清单，源修订 `de34dfa5`）。
