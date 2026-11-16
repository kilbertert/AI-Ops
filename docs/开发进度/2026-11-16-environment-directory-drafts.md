# 环境目录草稿与管理引导基础

基线`158b9e58`，分支`feat/environment-draft-directory`，关联#1537，P1基础片。
已实现：同目录草稿SQLite账本、双revision并发检查、锁内重新核权、显式全局目录管理权限、空目录CLI引导，
管理API/同源桥和管理准备配置；保存不改变现役JSON、连接或租户准入。
依据：[ADR0267](../adr/0267-environment-drafts-before-admission.md)、[验收合同](../acceptance/environment-directory.feature)。

当前片只交付目录准备能力。P0实际模块与容量不因此完成；P1仍需旧配置投影收敛、统一管理会话，
P2独立Tab、P3安装及P4激活继续按原计划，不把后端草稿当页面或已开通结果。
验证见[回执](../validation/2026-11-16-environment-directory-drafts.md)。本轮未改生产配置或业务数据，
新配置的引导/备份和接口方法已写入[手册](../agents/environment-registry.md)。**未完成业务验收**。
