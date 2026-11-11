# 费用存在性v4英文先验与1.7.4候选

日期：2026-11-11；分支`fix/consumer-fee-presence`，base `02e55376`。
父会话英文H5真实验收发现parkFee零值被与occupyFee空值合并为未提供；本轮仅补自然规则。
冻结已发布v3并保留全部原约束，v4增加跨语言逐字段核对零收费/缺项；兼容迁移沿用既有机制。
账务reader、模型、运营文本及最终正文路径不改。

冻结v4先验后已获准推进1.7.4 commit/push/一个PR，尚未部署或正式发布。
现有flow/migration 27项、目录19项、Ruff check/format通过，完整v4文本与源码hash已冻结。
父会话英文完整明细12.57秒与停车/占位专项10.33秒均正确区分parking 0.00与occupancy未提供；
另一租户中文验收进行中。Ulink已正常恢复V23/rev31，另一租户V3/rev3保留，Gateway1.7.3健康。
v4冻结文本`b229955ccf22f797afa77f52b116d0e9c4c77eff2807b63b65c6affd48dfe4fd`未改，不宣称业务全面验收。
英文受管预览方法已入QA；父会话负责独立Hack核读、CI/Devin及后续交付，作者建PR后结束。
1.7.4定向105项、Ruff、llmdoc、wheel/sdist、Gateway镜像与容器导入通过，v4冻结文字未改。
范围与恢复时点见[验证记录](../validation/2026-11-11-consumer-fee-presence.md)。
