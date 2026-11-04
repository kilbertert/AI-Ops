# 213. 出站端点 URL 的安全规则只有一处定义

日期：2026-11-04
状态：已采纳

「https 哪里都行，http 只给回环」这句话已被写进代码注释，却被逐字抄了五份、各带一条
错误文案；另有三类**携带服务凭据**的端点（kb-service 检索、Dify console 导出、
发布前的知识绑定校验）整条不检查它。「什么算回环」还有第二种渲染
（`ipaddress.ip_address(host).is_loopback`），对同一个输入给出相反答案：
字面量集合放行 `localhost` 但拒绝 `127.0.0.2`，`is_loopback` 正好相反。

## 决策与依据

- **`is_loopback_host(host)` 是「本机」的唯一答案**，取两种旧渲染的**并集**：
  `localhost`（不区分大小写，含 `localhost.`）或 `ipaddress.is_loopback`。
  取并集而不是二选一：两种渲染各自能正确识别对方漏掉的一类，取交集会把今天能用的
  配置变成拒绝。
- **`require_secure_endpoint(url, *, what)`**：绝对 http(s)、无 userinfo/query/fragment、
  https 除非回环。`what` 只用于文案。
- 五份副本改调 `is_loopback_host`；UPMS / Dis / kb-service / Dify console / 发布校验
  补齐明文半条。`tdengine_proxy` 的两处回环判定改用同一函数。

**这是一处行为变更，显式列出**：`http://<非回环>` 的 UPMS、Dis、kb-service、Dify
console 与发布校验地址现在会在构造时被拒绝（此前接受）。今天的部署端点都是回环
（如 `docs/gateway.md` 的 kb 隧道），因此**不断言线上存在真实的明文泄露**。

## 备选方案

- **只把五份副本合并、不给缺失的三类补检查。** 被否：规则说「Anywhere」，实现只在
  五分之一的地方说；下次远程配置会被静默接受。
- **只补缺失、不合并副本。** 被否：同一条规则五份文案分叉，改一处漏四处没有信号。
- **回环定义二选一（只认字面量 / 只认 `ipaddress`）。** 被否：前者拒绝 `127.0.0.2`，
  后者拒绝 `localhost`，都会把今天能用的配置变成拒绝。
