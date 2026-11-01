# T13 第 I 项补做：管家端（员工端）真实会话

日期：2026-11-01。分支 `docs/t13-i-staff-entry`，基线 `14ac5e33`（验证时线上 Gateway
`1.0.0+9e456e918181`）。上一轮把 I 记成"缺员工端凭据"，本次证明那是**取证结论错误**，
不是系统缺条件。

## 提交

- `docs/validation/2026-11-01-t13-i-staff-entry.md`（证据全文）
- `docs/validation/2026-11-01-t13-g1-i-d-evidence.md`（I 节由"未执行"改为"已补做并通过"）
- `docs/validation/2026-11-01-t13-step5-channels-and-al-matrix.md`（I 行升格；统计按 18 行重数）
- `docs/agents/runtime-retirement-operations.md`（新增：入口头会被管家主机 nginx 改写）

## 结论

| 子项 | 结论 |
| --- | --- |
| 员工端真实登录 | 通过（`/upms/token/login` 200，`CLOUD_ACCESS_TOKEN` 写入） |
| 员工端助手真实会话 | 通过（`chat_binding=operator_order_diagnosis:production-41:194210…`，rev 12，`matched`） |
| 跨租户订单 | 通过（被拒，403 `CPMS_INVALID_RESPONSE`） |
| 伪造 `tenant-id` 头 | 通过（无效，租户取自令牌） |

**I 项整体仍是「部分」，不是「通过」**：验收规则 I1/I2 要的是站点范围（空站点提示清楚、
受限站点订单可完成），而本轮只有一个 `data_scope.type=all` 的租户主账号——没有站点被限制，
也就没有东西可验。这条边界初版写漏，PR 复核指出后订正。

A–L 统计由 **通过 14 / 部分 3 / 未验证 1** 变为 **通过 14 / 部分 4 / 未验证 0**
（I 从"未验证"升到"部分"；脚本按 18 行逐条计数，与手写一致）。

## 两个取证错误

1. **上一轮把 `account+password` 资产当成消费者资产**：`aiops-ulink-account-20261030`
   的 `kind` 就是 `account+password`，正是管家 H5 的登录形态。同一个 `ulink` 账号
   2026-10-22 就在该域名上走过账号密码登录。凭据一直都在，是我读漏了。
2. **在管家 H5 上伪造 `X-Business-Entry` 得不出结论**：41 的
   `/etc/aiops-41/operator-h5-proxy.inc` 里有 `proxy_set_header X-Business-Entry operator`，
   请求还没出浏览器就被改写。看到"员工令牌 + consumer 头仍返回 202"时，先想到的是网关
   放过它；实际是 nginx 先把头换掉了。**只看网关响应会得出反的结论**，要读日志的
   `reason=` 或站点配置。

## 仍未覆盖

- 只验了 **Ulink 一个租户**的员工端，优链充员工端未验。
- D 项"原入口双端复验"仍未做——不过现在员工端有凭据了，它与消费者端各跑一次即可，
  是下一件能立刻做的事。
