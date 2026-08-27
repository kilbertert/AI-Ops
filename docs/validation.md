# 验证与验收计划

合成 fixture 可以验证确定性行为，但不能证明生产故障结论准确。所有“通过”都必须说明验证范围，不能把自动化回放写成工程师确认的业务验收。

## 基础设施边界

2026-07-31 已使用专用身份、无业务写入地验证生产访问路径：

- `aiops doctor` 通过受限 SSH 隧道连接 MySQL 8.4.7、TDengine 3.4 和 Redis 6.2.7。
- MySQL 只报告三个诊断表的 `USAGE` 和 `SELECT`；未授权业务表查询被拒绝。
- TDengine 代理返回所需 stable，并以 HTTP 403 拒绝 DDL；禁止直接 SSH 转发原生 TDengine REST 端口。
- Redis 允许有界 Stream 元数据和读取命令，访问配置范围外的 key 被拒绝。
- SSH 身份拒绝 shell 执行，只允许向批准的 MySQL、TDengine 代理和 Redis 端点做本地转发。

这只证明连接性和权限边界，不证明真实故障结论。

## 自动化与只读回放

2026-07-31 的业务加固回放无业务数据写入：

- 历史加固阶段完成了 88 项自动化测试，包括 17 个针对性回归和 40-case 协议/状态/launch type 矩阵；当前分支测试套件已经扩展到 159 项。
- 生产 TDengine schema 已直接核对。camelCase 字段必须使用反引号，严格代理和 runtime 使用相同的 allowlist 查询。
- Redis Stream payload 可能包含非 UTF-8 字节；runtime 现在对有界原始字节匹配订单号，只暴露数量和元数据，并成功匹配保留的生产消息。
- 30 天有界回放覆盖 1,012 笔订单。已有 `tx_data` 的 operator 订单不再误判为 `missing_tx_data`，特殊计费路径也不再产生之前的宽泛金额不一致。
- 生产样本覆盖 operator YKC1.8、remote YKC1.6、OCPP、AYK、两轮车和状态 2 路径；MySQL、TDengine、Redis 均未出现来源失败。
- 回放发现 178 笔 operator 订单的状态写成正常结束，但 YKC 停止码显示异常。报告现在会展示状态/停止原因矛盾，而不是静默接受。
- 生产 TDengine 代理部署后仍以 HTTP 403 拒绝 DDL。

这证明了规则和抽样数据的一致性，不替代工程师确认的故障结论。

## Codex-native Harness 验证

2026-08-03 已在不产生业务写入的条件下验证 thin harness：

- 测试覆盖不可变 incident identity、私有 workspace、证据哈希、PII/密钥脱敏、有界工具依赖、结果验证、格式错误修复、超时/provider 中断、恢复和可插拔 key slot。
- YKC 金额不一致、交易数据缺失、OCPP 服务端计费三类 fixture 各执行三轮 scripted-agent 验证，incident identity、工具序列、证据 ID、结论类别、置信度和限制保持一致。脚本验证的是 harness 可重复性，不是模型业务判断。
- 注入 TDengine 故障、结构化输出错误、turn 超时和 provider 失败；失败证据会保留，来源失败后不允许高置信度，原 thread/run 可恢复。
- `agent-doctor` 依赖保持只读：MySQL 无不安全权限或未解析角色，TDengine 所需 stable 通过严格代理暴露，Redis 可达。
- 30 天有界聚合样本返回 1,252 个候选行；十条代表性路径覆盖 YKC1.8、YKC1.6、OCPP、HLHT、HW104、operator/remote/admin launch、order type 0/1 和 status 0/1/2/3/5。
- 三条脱敏生产只读路径（YKC1.8、YKC1.6、OCPP）完成证据管线；artifact 哈希、`0700` workspace、密钥和真实数据库/API secret 扫描通过。

此前某个 provider slot 返回过明确的 `429 INSUFFICIENT_BALANCE`。该失败无 traceback、无密钥泄露，run 状态进入 `interrupted`，`agent-resume` 成功复用同一 thread。

## 真实 Provider 验证

2026-08-03 使用独立的有额度 key slot 对同一 provider endpoint 验证：

- 首次 live turn 发现 Responses API schema 的 `required` 约束，以及服务器默认 `codex` credential-injecting wrapper 对受限 sandbox 不可见的问题；两者均已修复并加入回归测试。
- runtime 改用 Python SDK 固定版本的原生 Codex binary，权限 profile 只读取该确切 binary，不再把服务器 wrapper 传给诊断进程。
- 三次真实模型 fixture 运行完成：YKC 金额不一致为中置信度，交易数据缺失在三类直接证据后为高置信度，内部一致的 OCPP 计费正确返回 inconclusive。
- 三次脱敏生产只读运行覆盖 YKC1.8 operator、YKC1.6 remote 和 OCPP；模型自主选择工具，证据引用和置信度与空/受限来源一致，没有业务写入声明。
- 六次运行均进入 `completed`；证据哈希、`0700` workspace、`0600` 文件和 secret/PII 扫描通过，MySQL、TDengine、Redis 始终只读。

这些是 provider 和生产路径验证，不是工程师确认的真实故障验收。

## Windows/Linux 便携包验收

最终便携化 PR 的 GitHub Windows runner 已通过：

- Windows stdout/stderr UTF-8、当前用户 owner、受保护 DACL 和冻结 launcher 子进程路径均经过真实失败后修复。
- 最终 ZIP 从源码目录之外解压，以最小 PATH 运行 `--help`、`init`、`paths`、`key-install`、`agent-doctor`、Codex 0.144.4 和三份 fixture。
- 私有配置、key、Codex home 和 run 目录的权限检查通过；制品不存在 `.key`、`production.env` 或 `auth.json`。
- Windows artifact 已上传并保留 7 天；用户本机最终 ZIP 验收见下节。

### Windows 便携包最终本机验收（2026-08-04）

在用户 Windows `D:\AI-Ops` 上从提交 `581c396` 构建最终 ZIP，并在源码目录之外的
`D:\AI-Ops-Portable-Acceptance-20260804` 解压运行：

- `--help`、`init`、`paths` 和 `agent-doctor --key-slot primary` 均通过。
- `agent-doctor` 返回 `base_url=https://api.psydo.top/`、`windows_sandbox=unelevated`、`business_mutations=disabled`。
- 最终冻结包使用外部私有 `primary` key 调用真实 provider，执行 `ykc_amount_mismatch.json` 合成 fixture；run `run-20260803T165338Z-65cd657e-269e` 完成 `diagnosed/medium`，摘要识别出设备 `totalFee=1.00` 与平台 `total_amount=1.20` 的 0.20 差异。
- 解压制品未发现 `.git`、`.key`、`auth.json` 或 `production.env`；运行事件文件未发现 `sk-` 密钥内容。
- ZIP：`D:\AI-Ops\dist\aiops-diagnostics-0.1.0-windows-x86_64.zip`；SHA-256：`22A16A2B105767493DB82F03862C9B3143D410817EFCBDCCA31FE62830062FEE`。

这证明 Windows 便携包、外部 key slot、API provider 和只读诊断链路可以在目标机运行；fixture 是合成数据，不能写成真实故障业务验收或准确率结论。

### Windows 便携包重复验收（2026-08-04）

在新的源码外解压目录 `D:\AI-Ops-Acceptance-20260804-2` 重复执行完整验收：

- ZIP SHA-256 仍为 `22A16A2B105767493DB82F03862C9B3143D410817EFCBDCCA31FE62830062FEE`；解压 196 个文件，未发现 `.git`、`.key`、`auth.json` 或 `production.env`。
- `agent-doctor` 再次确认 `base_url=https://api.psydo.top/`、`key_slot=primary`、`windows_sandbox=unelevated` 和 `business_mutations=disabled`。
- 三次真实 provider fixture run 均完成：OCPP `run-20260803T175242Z-3da3f4e6-7f74` 为 `diagnosed/high`（正常计费）；YKC `run-20260803T174918Z-06f526e0-db9a` 为 `diagnosed/medium`（0.20 金额差异）；交易数据缺失 `run-20260803T175424Z-64e42afe-19b9` 为 `diagnosed/medium`。
- 三个 run 均有 `diagnosis_completed` 事件；事件日志未发现 `sk-`、退款/重算/补发/重启/订单修改/消息重放等执行痕迹；验收结束后没有残留 `aiops.exe` 进程，primary key ACL 仍归当前 Windows 用户。

SSH 调试通道直接传入中文字面量会受远程代码页影响；本轮真实 provider 调用使用显式 `--order-no`，不将该传输限制误判为便携程序问题。构建 smoke test 和 Windows 进程内部中文解析已覆盖中文输出与反馈路径。

## M4 文档治理验证

2026-08-03 在 `docs/chinese-governance` 分支完成：

- 根目录 `README.md`、`docs/`、`ops/README.md` 和 `.env.example` 注释改为中文优先，命令、环境变量、JSON 字段和代码标识保持可复制、可检索。
- 新增仓库级 `AGENTS.md`，强制每个里程碑同步 `docs/开发进度.md`、`docs/validation.md`，并在入口、配置、部署或安全边界变化时同步 README、架构和部署文档。
- 进度文档已记录本次分支、里程碑状态、既有 CI/artifact 证据、`D:\AI-Ops` 未完成状态和下一步；本节的确定性检查结果将在提交前以实际命令输出为准。
- 本轮只修改文档、配置模板注释和治理规则，不改变业务代码、数据库权限或诊断动作边界；没有新增真实故障业务验收结论。
- 本轮确定性检查：`uv run pytest -q`（155 项通过）、`uv run ruff check .`、`uv run ruff format --check .`、`uv lock --check`、`uv pip check`、`uv run python -m compileall -q src tests packaging` 和 `git diff --check` 均通过。
- OpenCodeReview delegation preview 将 Markdown、`.env.example` 和新增 `AGENTS.md` 标记为 `unsupported_ext`（0 个可自动选取的 reviewable 文件）；已按同一默认规则完成人工差异审查，未发现需要修复的文档、安全或边界问题。

## M5 Windows runner 证据交付验证

2026-08-03 在 `D:\AI-Ops` 真实 Windows 源码工作区执行：

- Git bundle 恢复到提交 `0c15d87`，本地分支为 `test/windows-runner-validation-v2`；新增的 key、`auth.json`、`artifacts`、`runs` 和 `.aiops` 均已加入本地 `.gitignore`。
- `uv sync --locked --dev`、CLI help、155 项 pytest、`uv lock --check`、`uv pip check` 和 `compileall` 通过。
- `agent-doctor --key-slot primary` 通过，确认 `base_url=https://api.psydo.top/`、Windows `unelevated`、业务 mutation disabled；没有使用 Windows Codex App 官方账号额度。
- `run-20260803T124243Z-65cd657e-5a88` 和临时 `elevated` 对照 run `run-20260803T124847Z-65cd657e-11d3` 均成功采集 order/fee evidence，但最终被本地 staged-artifact 读取失败阻断；没有业务写入。该失败促成了 payload 交付修复。
- 修复分支 `fix/windows-model-evidence-delivery` 分发后，`run-20260803T130133Z-65cd657e-d07d` 在默认 `unelevated` Windows sandbox 下完成 `diagnosed/medium`；结果引用 `ev-001`、`ev-002`、`ev-003`，状态 `completed`，事件日志不包含 evidence payload 或 API key。
- 该 run 证明 Windows provider、Codex native runtime、只读工具、脱敏 payload 交付和结果验证链路可工作；它仍是合成 fixture，不是工程师确认的真实故障业务验收。

## 运行中进度与心跳验证

2026-08-04 使用真实 provider 对 OCPP 合成 fixture 做了进度流 smoke test：

- 运行 `run-20260803T194509Z-de1ce5e2-263c` 完成 5 个 Codex turn、3 个工具批次和 1 次结构化结果合同修复；最终状态为 `inconclusive`，符合 fixture 的业务边界。
- 将终端输出拆分为 stdout/stderr 后，stdout 通过 `jq` 解析为单一 JSON；stderr 实时输出启动、thread、turn、工具批次、合同修复、完成和 144 个心跳。
- 同一批事件同时写入私有 `events.jsonl`；事件仅含状态、计数和标识元数据，不含 evidence payload、API key 或数据库密码。
- 运行目录为 `0700`，事件和证据日志为 `0600`；`--no-progress` 的行为由代码路径保留事件、隐藏显示。
- 当前 159 项 pytest、Ruff、格式、compileall、锁文件和 diff 检查通过。

这证明了运行可观测性和追溯链路，不代表 fixture 或模型调用已经完成真实故障业务验收。

## Gateway MVP 集成验收

2026-08-04 在服务器本机启动临时 Gateway（仅监听 `127.0.0.1`），使用服务器私有 provider key 和 YKC 合成 fixture 完成端到端验证：

- 一次性 enrollment code 兑换成功；SQLite/WAL 只保存 code/token 哈希，客户端 token 在 Linux 测试环境以私有文件 fallback 保存，未进入源码或制品。
- 客户端通过 `GatewayClient` 创建 run、轮询增量事件并读取最终结果；run `run-20260804T020540Z-9a56710a-06cb` 完成 `diagnosed/high`，事件 34 条，包含 heartbeat、tool batch 和完成事件。
- `aiops remote doctor`、`aiops remote runs` 通过同一设备 profile 查询 Gateway；同 workspace 的第二设备可读取相同 run/event 数据，跨租户请求返回 `403`。
- Gateway API 测试覆盖健康检查、一次性注册、设备撤销、workspace 隔离、租户隔离、增量事件和 SSE 结束事件；新增 profile 路径穿越与 run 元数据脱敏回归测试，当前测试套件为 169 项通过。
- GitHub Actions CI run `30876603911` 的 Linux `verify` 与 Windows `windows-verify` 均通过；Windows runner 从最终 ZIP 完成构建、解压烟测并上传 artifact `8879776016`。

该验证没有连接生产数据库，也没有真实故障案例；它证明 Gateway 的身份、状态同步、真实 provider 执行和只读边界可以工作。公网生产部署仍需 TLS、OIDC Device Flow/mTLS、Vault/KMS、PostgreSQL、速率限制和审计保留策略。

## Gateway 受控跨端 Windows/Linux 验收

2026-08-04 使用服务器 Tailscale 节点和 Windows 节点 `rl` 完成受控跨端入口验收。服务器临时使用 Tailscale CA 签发的 Let’s Encrypt 证书监听独立 `9444` TLS 端口；原有 `8443 -> 8093` 路由未修改，验收结束后临时监听、注册码、设备令牌、Gateway DB 和证书私钥均已清理。

- Windows 节点 `rl` 在线，服务器通过 tailnet 连续收到 3 次 ping；当前链路经 DERP 转发，未建立 direct connection。
- Linux 客户端通过真实 HTTPS URL 完成 `remote enroll`、`remote doctor` 和 `remote diagnose`。首个 provider key slot 返回 `429` 后，切换同一 base URL 下的受控 `psydo-funded` slot；run `run-20260804T035406Z-32305a3c-9390` 完成 `diagnosed/medium`，同步 36 条事件并识别设备总费用与平台金额相差 `0.20`。
- 第二个登记为 Windows 平台的设备身份通过同一 GatewayClient 协议读取同 workspace 的 run 和事件；跨租户创建 run 返回 `403`，撤销设备后返回 `401`，重复兑换注册码返回 `400`。
- Gateway DB 和客户端私有目录权限分别为 `0700/0600`；数据库未发现注册码、设备 token、provider key 明文，诊断合同没有 evidence payload 或服务器绝对路径。
- 最终 ZIP 烟测新增 `remote --help`，并通过本地 mock Gateway 完成 `remote enroll`、`remote doctor`、`remote runs`，同时校验 Gateway profile/token 私有权限；Linux 与 Windows 源码外解压 ZIP 均通过。

结论：Gateway 的 TLS 入口、设备注册、跨设备 run/event 同步、租户隔离和撤销链路验收通过。该结论不等于用户 Windows 本机最终 ZIP 已完成实机运行，也不等于真实故障业务准确率验收；用户 Windows 本机运行与问题反馈是下一阶段。

## Windows Gateway 实机反馈与错误可见性回归

2026-08-04 用户在 `D:\aiops` 执行 `remote enroll` 时把实际文件 `D:\gateway-enrollment-ops.code` 写成了不存在的 `D:\gateway-enrollment.code`。Typer 在本地文件检查阶段返回 `File ... does not exist`，注册码没有因此被消费；随后使用正确文件名完成了 `DESKTOP-O8VJDOO` Windows 设备注册。

同一设备执行 `remote diagnose "订单号2084483071036829697有问题"` 时，客户端只显示 `gateway_run_queued`、`gateway_worker_started`、`gateway_run_interrupted` 和 `状态: interrupted`。根因是 Gateway 过去只持久化 `error_type`，未向 run 结果返回脱敏错误消息。修复后新增 `error_message` 字段、事件字段和客户端渲染，并增加旧 SQLite 表的自动迁移。

新增回归：Gateway store/API/client 相关测试 `11 passed`，全套测试 `173 passed`；测试覆盖错误类型和错误消息持久化与脱敏限长、注册码哈希、设备撤销、workspace/tenant 隔离、SSE 和 profile 权限。

服务端进一步确认首次中断发生在 Codex key 解析前：用户级 systemd 环境的 `XDG_CONFIG_HOME=/home/claude/.config` 使默认 key 目录被解析为 `/home/claude/.config/keys`，而实际私有 key 位于 `/home/claude/.config/aiops-diagnostics/keys`。Gateway 私有环境现已显式设置 `AIOPS_CONFIG_HOME` 和 `AIOPS_DATA_HOME`，重启后可进入 Codex thread 和证据工具执行。

真实 provider 回归中，`psydo-funded` 返回明确的 `401 API_KEY_DISABLED`，新版客户端/JSON 已显示脱敏错误原因；切换同一 base URL 下的 `psydo-primary` 后，run `run-20260804T112825Z-564b81e3-6402` 成功执行生产只读 `order_snapshot`，最终返回 `inconclusive/low`：在 `tenant-a` 范围内未找到订单 `2084483071036829697`。这证明错误可见性、provider 切换和生产只读入口已恢复，不代表该订单的业务故障已经验收。

边界：该订单号尚未连接真实故障案例并完成工程师结论比对；本节只记录客户端操作问题和错误追溯修复，不构成业务准确率验收。

## HttpSources 与 fixture 等价测试（2026-08-27）

对应 issue #36 / PRD T9，只验证 AI-Ops 侧 HTTP 客户端与离线等价行为：

- 新增 `tests/test_http_sources.py` 17 项自动化检查，覆盖 `/diag/order`、`/diag/device`、`/diag/gun-property`、`/diag/comm-message`、`/diag/redis-stream` 的路径、查询参数、`X-Internal-Token` HMAC-SHA256 与 `X-Request-Timestamp` 请求头、401/403 令牌失败、非成功 `code`、非法 UTF-8 响应封装、缺配置禁止发请求和非法标识符注入拒绝。
- `DiagApiSettings` 拒绝非 http/https 地址、URL 认证信息、query/fragment 以及越界的超时或令牌有效期。
- 三份 `examples/fixtures/`（ykc_amount_mismatch / ocpp_consistent / missing_tx_data）通过 mock HTTP transport 与 `FixtureSources` 对 orders / fee_template / device / gun_samples / comm_messages / streams 逐字段比对，结果一致。
- `AIOPS_DIAG_API_TOKEN_SECRET` 已加入 `Settings.redacted()` 回归，脱敏输出不含 secret。
- 检查命令：`uv run pytest tests/test_http_sources.py tests/test_sources.py tests/test_config.py tests/test_engine.py -q` 全部通过；`uv run ruff check` 通过；`git diff --check` 通过。
- 全套 `uv run pytest -q` 在合并验证前暴露出主线已有 `tests/test_gateway_client.py::test_gateway_client_does_not_retry_http_errors`：测试用 `HTTPError(..., fp=None)`，其 `.read()` 在 Python 3.11 返回 `str`，而 `_error_detail()` 原先按 `bytes.decode` 处理。已在合并中修复为兼容 `str`/`bytes`；重新运行全套 `uv run pytest -q` **241 项通过**。

未完成业务验收：本里程碑没有真实 Java `/diag/*` 服务或生产网络回放，不据此宣称业务查询准确率已经验收。

## AFK 工作流脚手架验证（2026-08-26）

纯工具链变更，不触碰诊断逻辑、打包产物或安全边界：

- 自动化检查：`pnpm install`（pnpm 11.15.1）通过，esbuild 的 build-script 门禁在 `pnpm-workspace.yaml` 的 `allowBuilds` 下通过；`pnpm afk` 无参数返回预期用法守卫、`pnpm ralph` 可加载并启动，证明 tsx + @ai-hero/sandcastle 依赖链可用。
- 镜像内容核验：`docker run sandcastle:ai-ops` 确认 python 3.11.2、uv 0.12.5、gh、claude-code、codex 0.146.1 就位，与 python 版 Dockerfile 一致（避免 agent 无法在容器内自检而误报 `<promise>BLOCKED</promise>`）。
- 未完成业务验收：AFK 是开发工作流工具，与订单诊断的业务准确率验收无关；真实故障案例验收仍按「业务验收待办」执行，不因脚手架合入而变更。

## Java `/diag/order` 工件契约验证（2026-08-27）

生产 Java 仓库远端部署，本仓库没有 JDK/Spring 构建链，因此 T1 的可验证边界是
团队仓库中的源码工件契约，而非部署服务后的真实环境行为：

- 自动化检查：`uv sync --group dev` 安装 dev 依赖；在合并 HttpSources 前 `uv run pytest` 全套 **224 项通过**，合并后最终 `uv run pytest` 全套 **241 项通过**；`uv run ruff check` 通过；`git diff --check` 通过。
- 新增 `tests/test_diag_order_contract.py` 的 15 项测试固定：`GET /diag/order` 路由、`X-Internal-Token` + `X-Request-Timestamp` 自校验、401 拒绝、`SAFE_VALUE` 注入拦截、`R<T>` 响应、`orders` 全字段且与 `sources.py` 参考列逐一一致、空 `tenant_id` 归一为跨租户 `null`、`fee_template.occupy_fee_template`、`LIMIT 3` 排序上限、审计切面按 `@RequestHeader` 排除请求头、HTTP 错误响应记为 failure、不落响应体、无默认硬编码 secret。
- 命令兼容性：任务约定命令 `uv sync --extra dev` 在本仓库失败（错误为 `Extra dev is not defined in optional-dependencies`），因为 `pyproject.toml` 将开发依赖声明在 `[dependency-groups]`，实际执行等价命令 `uv sync --group dev`，未跳过测试或静态检查。
- 未完成业务验收：Java 工件未编译、未部署、未对真实 `cloud-charging-pile-web` 执行 `docs/diag-query-api-plan.md` §11 的 Gherkin 场景；不把源码契约测试写成真实故障结论。

## HybridSources 部分切流量验证（2026-08-27）

对应 issue #50 / PR-A，验证范围是 AI-Ops 侧离线等价行为，不涉及生产 `production.env`、Java 服务或 tsdata：

- 新增 `tests/test_http_auth.py` 覆盖内部令牌算法与请求头；新增 `tests/test_hybrid_sources.py` 覆盖 HTTP/TDengine 路由、SQL 字面量、doctor 分类、`_safe_http_param` 白名单、长度边界与三份 fixture 逐字段等价；`tests/test_config.py` 覆盖 `Settings.http` 缺省、新旧环境变量优先级、令牌有效期下界校验与脱敏。
- 自动化检查：`uv sync --group dev`、`uv run pytest` **258 项通过**、`uv run ruff check`、`uv run ruff format --check .`、`uv lock --check`、`uv pip check`、`git diff --check` 全部通过。
- 任务约定 `uv sync --extra dev` 在本仓库失败，因 dev 依赖声明于 `[dependency-groups]`，实际执行等价命令 `uv sync --group dev`；未跳过检查。

未完成业务验收：本分支没有连接真实 `/diag/*` 服务或生产网络回放，2 路 TDengine 查询仍为直连；不据此宣称业务查询准确率已经验收。
## 方案文档 / AFK 工作流与 D 方案同步（2026-08-27）

对应 issue #52，纯文档变更，不改变运行时行为、生产凭据或用户入口：

- `docs/diag-query-api-plan.md` 已标注 D 方案为“已锁定”，并把收口计划从一次性删三库凭据改为分阶段 `2/3 → 1/3 → 0/3`；TDengine 段两个查询方法明确标记为“暂不进，等 tsdata 补洞后追加”，并在 §4 架构图、§6.2/6.3 接口和 §11.4 收口验收中保持一致。
- 新建 `docs/afk-cutover/decisions.md`，把团队记忆 `mem-20260827-ranlei-005` 明确标注为“候选期，待人工批准”，不把候选记忆写成正式决策结论。
- `docs/afk-workflow.md` 的 `ready-for-agent → dispatch-only` 文案已由 main 上的 PR #48 落地，本里程碑只确认同步，未再次修改该文件。
- 自动化验证：任务约定的 `uv sync --extra dev` 在本仓库因 `pyproject.toml` 无 `optional-dependencies.extra=dev` 而失败，按仓库既有约束改用等价命令 `uv sync --group dev`；合并后 `uv run pytest` 全套 **258 项通过**、`uv run ruff check` 通过、`git diff --check` 通过。
- 未完成业务验收：本里程碑没有真实 tsdata 补洞、没有生产凭据删除、也没有生产只读回放；不据此宣称 D 方案已经完成数据出口收敛。

## 内部令牌密钥配置化验证（2026-08-27）

对应 issue #42，验证范围是 AI-Ops 内部令牌密钥从配置读取、缺密钥启动报错、300s 验证窗口与双 key 过渡的离线行为；未连接真实 `/diag/*` 服务：

- 新增 `validate_internal_token()`，测试覆盖当前令牌通过、301s 过期拒绝、窗口边界、伪造/缺失头拒绝、非法时钟与有效期、空/缺失 secrets 的 fail-closed 行为，以及旧 key 与新 key 双 key 轮换；`Settings.http.internal_token` 默认窗口为 300s，密钥 `None` 且未回退到任何硬编码默认。
- `HttpSources` 构造时校验 secret，未配置 secret 时直接报 `Diag API 内部令牌密钥未配置`，测试确认不会发出 HTTP 请求。
- 自动化检查：`uv sync --group dev`、`uv run pytest` **268 项通过**、`uv run ruff check`、`uv run ruff format --check .`、`uv pip check`、`uv lock --check`、`git diff --check` 全部通过。
- 任务约定 `uv sync --extra dev` 在本仓库失败，失败原因为 `extra 'dev'` 不在 `optional-dependencies` 中；按仓库既有约束使用等价命令 `uv sync --group dev`，未跳过检查。

未完成业务验收：双 key 轮换未在真实 Java 服务上配合轮换演练，300s 窗口也未用真实服务和时钟偏移场景验收；不据此宣称生产密钥轮换已经完成。

## 业务验收待办

生产业务验收仍需要每条支持路径至少三笔由工程师确认结论的真实故障：

1. YKC 金额或电量不一致。
2. 订单结束后缺少交易数据。
3. 状态 2 不可控异常。
4. 状态 5 协议上报异常结束。
5. OCPP 服务端计费。
6. Redis 下游同步问题。

每个案例都要比较生成的摘要、分类、证据和下一步建议与工程师最终结论。即使建议碰巧正确，错误的高置信度仍然算失败。
