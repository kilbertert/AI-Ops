# 知识层增量同步（post-#1491，ADR-0251）

## 目标

- 跟随 `587af5d7`（运行日志行级明细 v2）与 `402e7e4d`（部署回执）把 llmdoc 知识层从
  `f99565f5` 基线推进到当前 HEAD 语义。

## 本轮动作

- `delta`：12 篇受影响 + 1 篇 needs-review；逐一判读，5 篇改写、7 篇核验未变。
- `validate`：通过；`commit`：docs `9c5184e` + meta `c22094f`。
- Devin 复审修订：`operator-chat.mdx` 的 `tools=N` 措辞明确为 0 时不落 detail。

## 交付与边界

- 改写面：`gateway-store`（版本闸语义）、`execution-delegation`/`operator-chat`（行级摘要
  与明细码）、`dify-self-service`（管理面明细一句）、`delivery-and-hosts`（补丁路径登记）。
- `execution-node-contract` needs-review 与其他 web-patch 目录级映射留待后续专项。
- 验收明细：[2026-11-12-llmdoc-sync-post-1491](../validation/2026-11-12-llmdoc-sync-post-1491.md)。
