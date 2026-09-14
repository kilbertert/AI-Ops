# 生产访问边界

TDengine Community Edition 3.4 允许非超级用户写入已有数据库，并且拒绝
`GRANT READ`。因此生产诊断不得通过 SSH 直接转发到 6041 端口。

`aiops-tdengine-readonly-proxy.service` 运行在数据库主机 loopback 接口上，只接受本项目发出的精确
`SHOW STABLES` 和有界 `SELECT` 语句。上游 TDengine 凭据只保留在该主机。Phase 3a 后工程师使用的 SSH key 只能转发到 TDengine 只读代理；MySQL / Redis 证据改由 `/diag/*` HTTP 诊断接口读取，不再保存直连凭据。

代理必须使用 root 所有的环境文件部署。客户端凭据和上游 TDengine 凭据都不得放入本仓库。回滚生产访问路径时，必须一并移除 service、service account、上游用户和 SSH `permitopen` 配置。

# 环境清单与 Agent 收敛（ops/environments/）

`ops/environments/<env>.toml` 声明一个环境应有的已发布客服/运维 agent。
`aiops admin reconcile` 通过 AgentManager（与 /v1/agents HTTP API 完全相同的
角色校验、模型白名单、发布前 KB 活性校验代码路径）把 gateway 库幂等收敛到清单
——取代环境 UPMS 未建 ROLE_AGENT_ADMIN 角色族时的 on-box sqlite 手工插库。

```text
[[agents]]                  # 每个期望的 agent
tenant_id / name / description
agent_type                  # customer | operations
prompt                      # 1-8000 字符（发布时校验）
knowledge_base_ids = [...]  # 发布时对 kb-service 做活性校验
model                       # 必须在 Settings.providers 白名单内
output_contract             # 可省，按 agent_type 推导（customer→blocks-v1）
opening_questions / quick_commands = [...]   # 可省
state = "published"         # published | disabled，默认 published
```

on-box 收敛（41 为例）：

```bash
aiops --config /etc/aiops-41/production.env admin reconcile \
  ops/environments/env-41.toml \
  --db /var/lib/aiops-41/gateway/gateway.db \
  --kb-url http://127.0.0.1:29380
```

`--db` 必须显式给出：`--config` 只喂 Settings（模型白名单等），数据库
路径走 `GatewayServerSettings.from_env()`，仅读进程环境变量。不 source
production.env 就执行时，会回退 XDG 默认
（`~/.local/share/aiops-diagnostics/gateway/gateway.db`）**静默新建空库**，
收敛结果全 `created` 而非预期 的 `unchanged`。

- 首跑应全部 `unchanged`；出现 `updated` 说明清单转写有误，停手修清单。
- `--prune` 仅作用于清单中出现过的租户（多余 published → disable，未发布
  draft → delete），绝不动清单外租户；`--dry-run` 零写入输出计划。
- 清单内模型不在白名单时在任何写入前失败（exit 2，库零变更）。
- 写的是运行中 gateway 的 sqlite（WAL + 短事务），首跑建议低峰窗口并先
  `cp gateway.db gateway.db.bak-<date>`。
