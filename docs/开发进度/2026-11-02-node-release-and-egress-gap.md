# 节点发版：schema 修复上线，暴露 CPMS 直连缺口

日期：2026-11-02。分支 `docs/node-egress-gap-record`，基线 `2a59a18e`。

## 已完成

- #1296（网关按声明下发站点工具）CD 自动部署
- #1299（节点 schema 改从声明生成）按 README「仅更新 Java 节点 jar」手工发版，
  release `2a59a18e` / jar `6e9a8a43…`（与锁逐字一致）
- **schema 漂移已修复**：订单诊断恢复 `completed` / `succeeded`

## 暴露的问题

站点查询失败，`CpmsRefused: cpms_unavailable`。节点容器到公网 CPMS 不通
（`api.mall.qushiyun.com` 与直连 IP 均 000），到内部网关 `172.30.41.1:8788` 连通。

旧 jar 会话回合 `tools=[]` 从不构造 `CpmsReadTool`，这条路径从未被执行。

## 一个操作失误

回滚节点到旧 jar 导致**订单诊断也挂了**——#1296 与 #1299 不可拆。
已把新 jar 装回。回滚前必须确认两处变更能否独立回退。

## 下一步

采用方案 B：管家工具改走 `proxy_base_url`，复用消费者端已有的
`/v1/internal/consumer-tools/` 代理，不为节点开公网出口。

## 验证记录

`docs/validation/2026-11-02-node-release-and-cpms-egress-gap.md`。
