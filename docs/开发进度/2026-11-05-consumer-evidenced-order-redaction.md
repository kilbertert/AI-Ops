# 客服正文保留已取证订单号

1.2.2原H5组合查询状态/账务成功，但通用脱敏器将19位本人订单号抹成REDACTED。
修复复用redact_text(preserve=...)，仅将同轮及已授权历史证据中的订单标识传入。
未知长号与电话仍脱敏，不改变工具访问权限。候选1.2.3，分支fix/consumer-order-answer-redaction。
