# AI-Ops 充电订单诊断

本项目是面向充电订单故障的只读诊断运行时。工程师可以直接提交自然语言反馈，例如：

```text
订单 2079842220423700481 金额不对，帮我排查
```

运行时会提取订单号和问题意图，在 MySQL、TDengine、Redis 上执行有界查询，结合生产后端源码整理出的业务规则，最后返回带证据的诊断报告。

> 第一次使用？看 [快速上手](docs/快速上手.md)，5 分钟从安装到跑出第一份离线诊断报告。

当前以 `aiops diagnose` 为统一入口：

- 默认走 Codex-native agent 路径：Codex 选择受限的只读证据工具并作出因果判断；Python harness 只负责事件身份、查询上限、证据日志、脱敏、置信度上限、结构化结果校验和恢复。
- `--mode deterministic` 是确定性已知 runbook 快速路径，用于稳定复现规则结果、离线 fixture、CI 回归，以及模型 provider 不可用时的基准对照。

开发进度、验证证据和未完成事项以以下文档为准：

- [快速上手](docs/快速上手.md)
- [开发进度](docs/开发进度.md)
- [验证与验收](docs/validation.md)
- [系统架构](docs/architecture.md)
- [Gateway 架构与部署](docs/gateway.md)
- [Codex thin harness](docs/thin-harness.md)
- [便携部署](docs/portable.md)
- [打包与下载](docs/打包与下载.md)

## 安全边界

- 不提供 `UPDATE`、`DELETE`、`INSERT`、DDL、退款、重算、补发、消息重放或服务重启能力。
- MySQL 查询在只读事务中执行，并使用固定的参数化 SQL。
- TDengine 查询必须包含设备、时间范围、选定列和 `LIMIT`。
- Redis 只允许读取元数据和有界的反向范围；不消费游标。
- 凭据只能来自环境变量或私有配置，所有输出都会脱敏。
- 报告不输出用户 ID、VIN、卡号、车牌号和原始协议 payload。
- Codex 通过可插拔的 Responses API provider 运行，支持多 provider 注册表（`AIOPS_PROVIDERS`）与默认 provider；默认不绑定单一供应商。`--key-slot` 选择 `AIOPS_CODEX_KEY_DIR` 下权限为 `0600` 的私有 key 文件；API key 不会进入 Git 或运行制品。

资产盘点发现的生产应用账号权限过大，本运行时不会使用它。已验证的部署使用专用 MySQL、Redis 和 SSH 身份；更换应用账号必须单独完成依赖审计和凭据轮换。TDengine Community Edition 无法提供数据库级只读角色，因此生产诊断必须使用 `ops/` 中的严格 loopback-only 查询代理。

## 安装与配置

安装开发依赖：

```bash
uv sync --dev
```

初始化平台私有路径并安装可插拔 provider key：

```bash
uv run aiops init
uv run aiops key-install primary
uv run aiops agent-doctor --key-slot primary
```

使用 `aiops --config /path/to/production.env ...` 选择另一份私有配置。`aiops paths` 会输出解析后的非敏感路径。

## 诊断命令

运行离线合成示例（确定性，无需 provider key）：

```bash
uv run aiops diagnose "订单 TEST-YKC-0001 金额异常" --mode deterministic --fixture examples/fixtures/ykc_amount_mismatch.json
```

只检查实时数据源连通性，不读取订单：

```bash
set -a
source /path/to/production.env
set +a
uv run aiops doctor
```

诊断实时订单（默认走 Codex agent，需已安装 provider key；无需传 `--tenant-id`，工具按订单号自动发现租户）：

```bash
uv run aiops diagnose "订单 2079842220423700481 中途停止" --key-slot primary
```

输出机器可读 JSON：

```bash
uv run aiops diagnose "订单 2079842220423700481 金额异常" --key-slot primary --json
```

对合成案例运行 Codex-native 诊断（`diagnose` 默认即 agent 模式，加 `--mode deterministic` 可切换确定性规则）：

```bash
uv run aiops diagnose "订单 TEST-YKC-0001 金额异常" \
  --fixture examples/fixtures/ykc_amount_mismatch.json \
  --key-slot default --json
```

`diagnose` 默认把运行阶段、工具批次、合同修复和 Codex 心跳输出到终端；`--no-progress` 只关闭当前终端显示，不会关闭私有 `events.jsonl` 记录。使用 `--json` 时，诊断 JSON 保持在 stdout，进度改写到 stderr，并在结果中返回 `events_path` 和 `evidence_journal_path`。

心跳间隔由私有配置中的 `AIOPS_AGENT_HEARTBEAT_INTERVAL_SECONDS` 控制，默认 10 秒。事件日志不包含 evidence payload，只保留阶段、状态、turn、工具名称和计数等追溯元数据。

中断后恢复同一事件/thread，也可以在同一 provider endpoint 下切换另一把 key：

```bash
uv run aiops agent-resume RUN_ID --key-slot backup --json
```

恢复时会比较当前配置的 `AIOPS_CODEX_BASE_URL` 与运行记录的 endpoint，拒绝把保存的 key 重定向到另一台主机。fixture 会复制到私有运行目录并在恢复时校验哈希；模型 sandbox 不能读取 fixture 或 harness 控制文件。

启动交互式 shell：

```bash
uv run aiops shell
```

## Windows 与 Linux 便携制品

便携制品包含 Python 应用、整理后的诊断参考资料、合成 fixture，以及 Python SDK 固定版本携带的平台原生 Codex runtime；不包含 API key 或生产数据库凭据。

必须在目标操作系统上构建：

```bash
uv sync --locked --dev
uv run python packaging/build_portable.py
```

命令会生成 `dist/aiops/` 和版本化 ZIP，并在源码目录之外解压后，以最小 `PATH`、隔离私有 home、内置 Codex runtime、三份离线 fixture 和本地 mock Gateway 做烟测；Gateway 烟测覆盖 `remote enroll/doctor/runs`。Windows 11 是推荐部署基线；OpenSSH、sandbox、ACL 和首次运行要求见[便携部署文档](docs/portable.md)。构建新包与从 CI 下载 Windows 制品的完整步骤见[打包与下载](docs/打包与下载.md)。

Windows 便携包连接中央 Gateway 的最短操作路径：

```cmd
cd /d D:\aiops
dir D:\gateway-enrollment-ops.code
aiops.exe remote enroll --url https://aiops.example.com --code-file D:\gateway-enrollment-ops.code
aiops.exe remote doctor
aiops.exe remote runs
aiops.exe remote diagnose "订单 123 金额异常" --json
```

`remote enroll` 只运行一次；`remote doctor` 只检查公开健康端点，`remote runs` 才验证设备令牌。完整的 Windows 路径、注册码、结果查看与错误追溯说明见[快速上手](docs/快速上手.md)和[便携部署文档](docs/portable.md)。

## 业务规则来源

- `SOP.md`
- `充电桩问题排查SOP.md`
- `backend-v2-domestic/cloud-charging-pile`

后端快照由 `.gitignore` 忽略，仅作为只读规则依据；公开仓库和便携制品不包含该私有源码。待确认 canonical remote 后，再考虑使用固定源码提交或 submodule 替代本地快照。

## 当前验收边界

目前没有工程师确认的真实故障结论。自动化测试、生产只读回放、真实 provider 调用和便携制品验收只能证明实现一致性与运行边界，不能替代业务准确性验收。必须取得真实案例并由工程师确认摘要、分类、证据和下一步建议后，才能记录业务验收结果。第一版仍明确不支持两轮车完整规则。

provider 使用 `.env.example` 中的 OpenAI-compatible Responses API 配置。provider 可能因额度或模型策略拒绝请求；这类失败会写入运行事件日志，并可在不改变事件 manifest 的前提下恢复。

## 多端 Gateway 模式

便携包不应直接携带数据库、SSH 或 provider 凭据。推荐由固定服务器运行 `aiops-gateway`，Windows/Linux 客户端通过一次性注册连接同一 workspace，使用 `remote runs` 和 `remote events` 同步 run 进度。详细的安全边界、配置和生产硬化要求见 [Gateway 架构与部署](docs/gateway.md)。
