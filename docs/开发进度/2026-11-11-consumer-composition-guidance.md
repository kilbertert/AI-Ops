# 订单构成事实约束v2

日期：2026-11-11；分支`fix/consumer-composition-guidance`，base `b208b246`，候选1.7.2。

父会话真实验收确认订单投影正确但自然回答仍擅加币种、误读null、自主算错合计及未经核验安抚。
已通过正常版本API由V24/rev28恢复V23/rev29；本轮保留原数据修复，仅补提示规则及迁移。

已将本任务事实规则升为v2并放到运营全文末尾，优先于安抚风格；禁止自主合计与未核验“无异常”，
缺币种时开头说明且所有金额无币种单位，null明确未提供。保留BASE、前后运营文本、模型/工具/库。
无规则/v1/v2三种图迁移幂等，未知或重复块拒绝；版本四处同步1.7.2，ADR0246补充后验限制。

最小回归101项通过；红灯与细节见[验收记录](../validation/2026-11-11-consumer-composition-guidance.md)。
Ruff、文档/目录25项、llmdoc校验、wheel/sdist、Gateway镜像与容器导入通过。
自然模型语义仍待父会话真实preview逐句核对，未完成业务验收；本地不重复全量，最终CI完整。
作者完成commit/push/一个PR后交接，父会话负责Devin、合并和CD；current-delivery-state由父最后写。
