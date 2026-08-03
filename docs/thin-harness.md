# Codex-native Thin Harness

## 控制权反转

`aiops diagnose` 仍然是确定性的已知 runbook 快速路径。
`aiops agent-diagnose` 使用持久 Codex thread 作为诊断主体：

```text
工程师反馈
       |
       v
不可变 incident manifest -> Codex thread
                               | tool_requests
                               v
                      有界只读适配器
                               |
                               v
                      私有证据日志
                               |
                               +---- 恢复同一 thread
                               v
                      结构化诊断结果
                               |
                      合同验证器
```

Codex 决定请求哪些证据以及如何组合证据。harness 不选择最终因果结论，只保证请求的操作合法、有界、限定到租户和订单、进入日志、完成脱敏并可恢复。

## Harness 不变量

- `IncidentManifest` 固定订单号、租户、反馈意图和来源哈希。
- `AgentWorkspace` 是私有目录（POSIX 为 `0700`，Windows 使用当前用户保护 DACL），包含暂存 SOP/业务参考，不包含生产凭据或跨运行原始状态。
- 合成 fixture 会复制到隐藏且带哈希校验的输入路径，恢复运行时不能悄悄读取变化后的测试数据。
- `EvidenceJournal` 为每个工具结果保存不可变 artifact、SHA-256、有界请求元数据和来源状态；依赖性读取会重新校验哈希。
- tool result 同时携带从 artifact 读取、已脱敏且有限大小的 evidence payload，模型不必依赖本地 sandbox 回读私有文件；artifact 仍是审计源，事件日志不重复写入 payload。
- `DiagnosticToolExecutor` 只提供订单、费用、设备、TDengine、Redis 和 advisory runbook 工具。不提供 SQL 输入、任意表选择、生产 shell 路径或业务动作。
- `AgentResultValidator` 拒绝身份漂移、缺少证据引用、只引用 known runbook 的结论、来源失败后的高置信度、密钥泄露和声称已执行禁用动作。它不会替模型重写根因。
- provider 失败、超时、格式错误或进程中断后，原有 `thread_id` 和 `run_id` 仍可恢复。

## Provider 与 key slot

provider 写入生成的私有 Codex home，使用以下配置：

- `AIOPS_CODEX_BASE_URL`：非密钥的 Responses API 地址。
- `AIOPS_CODEX_API_KEY`：可选的一次性环境变量覆盖。
- `AIOPS_CODEX_API_KEY_FILE`：可选的显式私有 key 文件。
- `AIOPS_CODEX_KEY_DIR/<slot>.key`：默认的可插拔 key slot 存储。
- `--key-slot`：在不改代码的情况下切换同一 base URL 的另一把 key。

POSIX key 文件必须属于当前开发账号且为 `0600`；Windows key 文件使用当前用户保护 DACL。key 只作为内部 provider 变量传给 Codex app-server，模型生成的 shell 命令收到的是过滤后的环境，不包含业务密钥。运行状态和日志只记录 slot 名称，`agent-doctor` 只记录短的一次性指纹。恢复运行会拒绝不同 base URL，只允许切换 key slot。

## 明确不做的事情

第一版没有重算、退款、补发、重放、订单修改、配置修改、数据库写入、服务重启或 remediation 工具。未来的动作面必须独立设计，具备明确的人审和自己的 mutation ledger。
