# 站点工具按站点身份校验

日期：2026-11-02。分支 `fix/site-tool-no-order-identity`，基线 `2a59a18e`。

## 一句话

`identity()` 对每行都要求 `orderNo`，站点行没有这个字段 → `cpms_scope_mismatch`
被 catch-all 吞成 `cpms_unavailable`，看起来像网络故障。改为按**有没有订单号**分流。

## 提交

- `CpmsReadTool.java`：`orderNo()` 允许缺失；`Grant` 允许空 `inputs`；`identity()` 按站点校验
- `CpmsToolAcceptance.java`：新增 `siteSearchHasNoOrderIdentity`（含两例越权拒绝）
- `docs/validation/2026-11-02-site-tool-identity-without-order.md`

## 验证

`CPMS_TOOL_ACCEPTANCE PASS assertions=195`。变异验证：恢复旧 `identity()` 后该用例变红。

## 弯路

先误判为「节点需要公网出口」——实际网关早已返回代理地址。
`cpms_unavailable` 是 catch-all，把 `cpms_scope_mismatch` 掩盖成了网络故障。

## 下一步

构建节点 jar 并按「仅更新 Java 节点 jar」发版；发版后复跑管家端站点问题。
