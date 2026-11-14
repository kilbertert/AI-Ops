# 客服选库与提示词发布修复上线

2026-11-14 UTC。PR #1514 合并abef9374，标准CD成功，1.10.1注释标签核实；
Dify Web consumer-selection-abef9374换发，旧镜像和配置保留。

优链充原受管页面移除旧失效引用，保留新库与产品当前提示词；预览13.37秒成功，
发布新应用V1/revision8，正式问答8.13秒成功并带新库引用。工作流和兼容快照一致。
最终CI5053通过/10跳过；实现定向114项、前端9项、独立恢复与并发2项通过。

本回执分支docs/consumer-publication-live，基线c9b311e9；运行代码已上线，本分支只归档。
详见[上线回执](../validation/2026-11-14-consumer-publication-selection-live.md)。
原生App/H5屏幕与知识内容业务准确性未验，不能称为全业务验收；下一步产品按原入口复测。
