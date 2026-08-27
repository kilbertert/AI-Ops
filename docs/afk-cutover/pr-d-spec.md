# PR-D：Phase 3a 收口 —— 删 MySQL+Redis 直连凭据，TDengine 留口子

**目标**：Phase 3a 只删除 `production.env` 中 MySQL / Redis 凭据及对应直连配置；AI-Ops 的生产诊断继续使用 `HybridSources`（4 个查询走 `/diag/*` HTTP，报文与时序继续走 TDengine 直连），TDengine 凭据和 SSH 隧道保留到 Phase 3b。

**关联 issue**：#43

**Blocked by**：PR-A / PR-B / PR-C（已经合入 main）

## 范围

- [ ] `.env.example` 不再给出 `AIOPS_MYSQL_*` / `AIOPS_REDIS_*` / `AIOPS_SSH_MYSQL_*` / `AIOPS_SSH_REDIS_*` 活动配置模板；只保留注释说明“已收口”。
- [ ] `HybridSources.doctor()` 将 MySQL / Redis 段明确标记为 `deprecated`，且文案不再声称生产可 fallback 到本仓直连。
- [ ] 生产诊断入口 `live_sources()` 继续默认 `HybridSources`，不因缺少 MySQL / Redis 凭据而启动失败。
- [ ] 文档与部署模板不再要求 OPS 保存或配置 MySQL / Redis 直连凭据；只保留 TDengine 直连代理、`/diag/*` HTTP 内部令牌和 provider key。

## 不含

- 不删除 `production.env` 中 TDengine 凭据和 SSH 隧道配置（Phase 3b 待 tsdata 补洞）。
- 不在本仓直接修改生产主机上的私有 `production.env`（仓库外文件由部署按回滚预案执行）。

## 验收

- [ ] `doctor()` 返回 `http=ok`、`tdengine=ok`、`mysql=deprecated`、`redis=deprecated`，整个过程不阻断诊断。
- [ ] `HybridSources` 下 `/diag/order`、`/diag/device`、`/diag/redis-stream` 走 HTTP；`/diag/comm-message`、`/diag/gun-property` 暂未提供，继续走 TDengine 直连。
- [ ] `aiops-gateway` 在只含 HTTP 内部令牌 + TDengine 凭据的服务器配置下可以启动，真实 provider fixture 端到端冒烟待具备真实 provider key 的环境执行。

## 回滚预案

删除生产 MySQL / Redis 凭据前，先把当前 `production.env` 的完整内容备份到仓库外路径（例如 `~/.config/aiops-diagnostics/backups/production.env.pre-phase3a-<日期>`，权限 `0600`）。回滚时只恢复备份文件并重启 `aiops-gateway` / `aiops` 进程，不修改仓库代码。

## 验证记录

自动化与离线等价验证写入 `docs/validation.md`；真实生产 `production.env` 的删除结果和真实 provider fixture 冒烟未在本仓执行时，必须明确标记为“未完成业务验收”。
