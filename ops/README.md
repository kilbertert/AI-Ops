# 生产访问边界

TDengine Community Edition 3.4 允许非超级用户写入已有数据库，并且拒绝
`GRANT READ`。因此生产诊断不得通过 SSH 直接转发到 6041 端口。

`aiops-tdengine-readonly-proxy.service` 运行在数据库主机 loopback 接口上，只接受本项目发出的精确
`SHOW STABLES` 和有界 `SELECT` 语句。上游 TDengine 凭据只保留在该主机。Phase 3a 后工程师使用的 SSH key 只能转发到 TDengine 只读代理；MySQL / Redis 证据改由 `/diag/*` HTTP 诊断接口读取，不再保存直连凭据。

代理必须使用 root 所有的环境文件部署。客户端凭据和上游 TDengine 凭据都不得放入本仓库。回滚生产访问路径时，必须一并移除 service、service account、上游用户和 SSH `permitopen` 配置。

# 环境清单与 Agent 收敛（ops/environments/）

`ops/environments/<env>.toml` 是**一个文件、两个不同权威的段**：`[[agents]]`
是**发布权威**，`[[dify_apps]]` 是**运行时注册表**。两者的定位不同，清单把它们
分开写而不是混为一谈：

- **`[[agents]]`（发布权威）** 声明一个环境应有的已发布客服/运维 agent。
  `aiops admin reconcile` 通过 AgentManager（与 /v1/agents HTTP API 完全相同的
  角色校验、模型白名单、发布前 KB 活性校验代码路径）把 gateway 库幂等收敛到清单
  ——取代环境 UPMS 未建 ROLE_AGENT_ADMIN 角色族时的 on-box sqlite 手工插库。
- **`[[dify_apps]]`（运行时注册表，不是发布权威）** 声明 `(租户, 业务入口)` 由
  哪个 Dify app 服务。它**不发布任何东西、也不版本化任何东西**：从 Dify 拉来的
  agent 仍然由我们自己的发布动作发布。它记录的只是运行时读的那条映射。
  Dify 里**完全没有租户概念**，所以租户判定只能在我们的运行时完成，这张表就是
  那条边界的落点。

```text
[[agents]]                  # 每个期望的 agent（发布权威）
tenant_id / name / description
agent_type                  # customer | operations
prompt                      # 1-8000 字符（发布时校验）
knowledge_base_ids = [...]  # 发布时对 kb-service 做活性校验
model                       # 必须在 Settings.providers 白名单内
output_contract             # 可省，按 agent_type 推导（customer→blocks-v1）
opening_questions / quick_commands = [...]   # 可省
state = "published"         # published | disabled，默认 published

[[dify_apps]]               # 运行时注册表（#584 / PRD #577）
tenant_id                   # 租户
business_entry              # consumer | operator（= faq.PLATFORMS）
app_id                      # Dify 侧 app id：拉取面（dify_dsl_pull）按它导出
agent_name                  # 该 app 落到我们库里的 agent **名字**
```

`agent_name` 是名字而不是 `agent_id`：id 每次重新发布都会变，而名字已经是
`agent_manifest` 的收敛键，也是同一个租户内唯一的。四字段**全部必填、没有一个
是推导出来的**——注册表的全部意义就是"运行时被告知哪个 agent 服务这个入口"，
而不是自己算出来。绑定的 `agent_name` 必须在本清单的 `[[agents]]` 里声明过
（同租户内），否则 `load_manifest` 在**任何写入之前**就失败：注册表行和它指向的
agent 是同一个事实，半个错的事实不能先落库。

**fail closed。** 注册表为空时（清单里没有 `[[dify_apps]]`）保持注册表出现之前
的选法不变——所以这个开关可以**惰性地**上线，未采用它的环境行为逐字不变。一旦
表里有**任何一行**，未登记的 `(租户, 入口)` 就**什么都不选**：不回退到最新已发布的
agent，也不落到零阶回答。回答是 `retrieval_status="unavailable"`（不是
`not_found`）——什么都没检索，说"库里没有"就是一个没人做过的断言。

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
- `[[dify_apps]]` 在同一命令里收敛（输出 `dify_registry` 段），与 agent 收敛
  是同一个已声明状态：拆成第二条命令会让两者在两次调用之间走偏。未变更的绑定
  不写库；库里有、清单里没有的绑定**只在 `--prune` 下删除**（与 agent 删除同一
  个开关，理由相同：删除是需要被要求的方向，多出来的绑定是**可见的**——运行时
  仍在服务它，报告也点它的名）。
- 清单内模型不在白名单时在任何写入前失败（exit 2，库零变更）。
- 写的是运行中 gateway 的 sqlite（WAL + 短事务），首跑建议低峰窗口并先
  `cp gateway.db gateway.db.bak-<date>`。
