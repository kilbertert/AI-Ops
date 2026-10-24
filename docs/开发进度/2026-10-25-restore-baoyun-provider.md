# M3 模型渠道恢复 baoyun

基线90a5a280，分支docs/restore-baoyun-provider。

按用户指示恢复原baoyun配置、凭据、节点执行清单和平台智能体模型绑定，删除Cline模型项
与运行凭据；开发机共享代理的Cline入口同时移除，其余渠道保留。

baoyun直连与实际受控出口均HTTP 200，后者答案非空且正常完成。平台六份状态读回通过，
本片不修改应用实现；详见[验收记录](../validation/2026-10-25-restore-baoyun-provider.md)。

下一步回到M3本人订单成功链验收，使用baoyun；不再使用Cline。
未完成业务验收，生产业务入口仍未启用。
