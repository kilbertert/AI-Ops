# 客服选库与提示词发布恢复

2026-11-14。任务分支 `fix/consumer-publication-selection`，初始基线 `693e5a0c`。
目标是消除授权与应用选库混淆、发布失败后修改提示词无法重试，以及状态误读旧配置。

已实现发布抽屉真实选库、失效引用移除、失败保存重试、当前正式应用提示；合同校验先于
Agent创建，原始系统半成品按应用归属恢复。保留租户权限、原生hash和路由revision。

定向80项、浏览器9项及临时Gateway通过，详情与首轮全量超时记录见
[验证](../validation/2026-11-14-consumer-publication-selection.md)。独立验收及完整构建进行中。
下一步整合最新主线、完成CI/审查、配套Gateway与Dify Web部署、原入口验证；未完成业务验收。
