# 验证与验收计划

合成 fixture 可以验证确定性行为，但不能证明生产故障结论准确。所有“通过”都必须说明验证范围，不能把自动化回放写成工程师确认的业务验收。

## C/B 固定问答接口验证（2026-09-04）

本轮实现使用真实 UPMS 只读数据库结构和离线 HTTP fake 验证平台边界：

- 真实只读探查确认 `qumall_upms.sys_user`、`sys_user_role`、`sys_role` 的关联字段可用；`client_type` 观察到 `admin`、`tenant-app`、`MA`、`supply-admin`，同一 C 用户存在多个 B 主体的真实记录。
- 生成器从未追踪的 `用户端.docx`、`管家端.docx` 提取 28/17 条固定问答，生成版本化目录与推荐 JSON；DOCX 不进入 Git。
- `tests/test_faq.py` 和 `tests/test_faq_gateway_api.py` 覆盖无 B 绑定、唯一/多 B 主体、入口隔离、前缀校验、额外字段拒绝、同步答案和无诊断副作用。
- #132 加固继续覆盖未知角色、跨租户映射、非法业务入口和不可信 `platform` 查询参数；身份与答案采用零缓存，避免陈旧授权。

当前结果：离线实现验证通过；真实 BFF 请求、真实 C 端会话和前端联调尚未完成，不能宣称生产平台权限或业务验收通过。

本轮 #133 交付验收证据：2026-09-04T03:30:06Z，提交 `370edf1` 的独立 checkout；FAQ 目录生成 28/17 条、专项测试 15 项、全量 pytest 511 项均通过，且构建制品包含两个 FAQ JSON。真实 BFF/生产会话仍待联调，未将离线 fake 当作真实权限结论。

测试部署证据：2026-09-04T04:07:31Z，用户级 `aiops-gateway.service` 运行部署提交 `a4702fa`。`https://aiops-api-test.ranlei.work/health` 返回 200；使用私有服务令牌、无效 thirdSession 和 `X-Business-Entry: consumer` 调用 `/v1/faq/recommendations` 返回 401 `INVALID_ACCESS_TOKEN`。这证明公网入口、服务令牌配置读取和 thirdSession 失效语义，未证明有效 C 端会话或生产 FAQ 内容权限。

真实 C 端补充验收：2026-09-04 使用当前业务 Redis 中有效会话（仅保留脱敏审计摘要）调用同一公网地址。consumer 推荐、目录和 `consumer.faq.q001` 答案分别返回 200，数量为 28/28，答案格式为 `text`；以 consumer 身份请求 `operator.faq.q001` 返回 404 `FAQ_NOT_FOUND`；请求 operator 入口返回 503 `PLATFORM_UNAVAILABLE`。固定问答过程中未创建诊断资源、未查询订单、未调用模型。

当前可解析会话中未找到唯一 B 端主体映射，operator 成功路径仍为阻塞，不能宣称管家端真实业务验收完成。

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

## doctor() 分类与 .env.example 注释化验证（2026-08-27）

对应 issue #51 / PR-B，验证范围限定在 AI-Ops 的离线 doctor 分类与示例模板注释，不涉及生产 `production.env`：

- 补充 `tests/test_hybrid_sources.py` 用例覆盖 `HybridSources.doctor()` 的 `http` / `tdengine` / `mysql` / `redis` 四段结构，以及 HTTP 探针的 `http.config_missing`、`http.auth_failed`、`http.http_unreachable` 分类（含 JSON 响应体 401/403/500 与缺失密钥/有效期场景）；失败结果不包含测试 secret 或 Diag base URL 字符串。
- 新增 `tests/test_env_example.py` 固定 `.env.example` 中三个 `AIOPS_HTTP_*` 新变量，以及 PR-B 要求的 TDengine `WARNING` 和 MySQL/Redis `DEPRECATED` 注释。
- 补充 `tests/test_render.py` 用例固定 `render_doctor()` 将 `deprecated` 渲染为 `DEPRECATED`。
- 自动化检查：`uv sync --group dev`；`uv run pytest` 全套 **268 项通过**；`uv run ruff check`、`uv run ruff format --check .`、`uv lock --check`、`uv pip check`、`git diff --check` 全部通过。
- 命令兼容性：任务约定的 `uv sync --extra dev` 在本仓库失败，因 dev 依赖声明于 `[dependency-groups]`；实际执行等价命令 `uv sync --group dev`，未跳过测试或静态检查。
- 未完成业务验收：HTTP doctor 探针未连接真实 `/diag/*` 服务，TDengine 段仍未回收凭据；不把离线分类测试写成真实故障或真实环境验收结论。
## Java `/diag/occupy-order` 工件契约验证（2026-08-27）

生产 Java 仓库仍在远端，本仓库没有 JDK/Spring 构建链，因此 T4 的可验证边界与
T1 一致，是团队仓库中的源码工件契约，而非部署服务后的真实环境行为：

- 自动化检查：新增 `tests/test_diag_occupy_order_contract.py` 的 14 项契约测试通过；全套 `uv run pytest` **292 项通过**，`uv run ruff check` 通过，`git diff --check` 通过。
- 固定契约点：`GET /diag/occupy-order` 路由、内部令牌自校验与 401 拒绝、`order_no` / `order_id` 恰二选一、`order_no → order_no` 与 `order_id → orderId` 列映射、`tenant_id` / `status` 可选过滤、`SAFE_VALUE` 注入拦截且先于查询执行、SQL 占位符与绑定参数数量一致、`R<T>` 列表响应、全字段列清单、`ORDER BY startTime DESC LIMIT 20`。
- 命令兼容性：任务约定命令 `uv sync --extra dev` 仍因 `pyproject.toml` 使用 `[dependency-groups]` 而失败（Extra `dev` 未定义），实际执行等价命令 `uv sync --group dev`，未跳过测试或静态检查。
- 未完成业务验收：Java 工件未编译、未部署、未对真实 `cloud-charging-pile-web` 执行 Gherkin 场景；不据此宣称占位费订单查询业务准确率已经验收，也不把契约测试写成真实故障结论。
## Java `/diag/redis-stream` 工件契约验证（2026-08-27）

对应 issue #40 / PRD T5，只在无 JDK/Spring 工具链的团队仓库内验证源码工件契约，不验证远端服务：

- 新增 `tests/test_diag_redis_stream_contract.py` 16 项测试，固定 `GET /diag/redis-stream` 路由、`X-Internal-Token` + `X-Request-Timestamp` 自校验、401 拒绝、白名单 Stream 校验与非法 Stream 400 拒绝、`max_messages` 1000 上限与非正值保护、有界 `XREVRANGE`、`R<List<...>>` 响应字段、消费组 `name/consumers/pending/lag` 字段与缺失 Key/畸形 `lastDeliveredId` 的空值防护。
- `order_no` 匹配计数继续在 `SAFE_VALUE` 白名单校验之后执行，且只扫描 `max_messages` 截断的消息窗口。
- 检查命令：`uv run pytest tests/test_diag_redis_stream_contract.py tests/test_diag_order_contract.py -q` 通过（16 + 15 项）；全套 `uv run pytest` **257 项通过**，`uv run ruff check` 通过，`git diff --check` 通过。
- 命令兼容性：`uv sync --extra dev` 仍因 `Extra dev is not defined in optional-dependencies` 失败；本仓库开发依赖位于 `[dependency-groups]`，实际以 `uv sync --group dev` 同步等价依赖后执行全部检查，未跳过测试。
- 未完成业务验收：本节只有自动化源码契约证据，未编译、未部署 Java 服务，未执行 `docs/diag-query-api-plan.md` §11 的 Redis Gherkin 场景，也不能据此宣称生产同步队列诊断准确率已经验收。
## Java `/diag/device` 工件契约验证（2026-08-27）

生产 Java 仓库远端部署，本仓库没有 JDK/Spring 构建链，因此 T6 的可验证边界同样是
团队仓库中的源码工件契约，而非部署服务后的真实环境行为：

- 自动化检查：新增 `[project.optional-dependencies] dev`（与 `[dependency-groups] dev` 同清单）后，任务约定命令 `uv sync --extra dev && uv run pytest && uv run ruff check` 直接通过；合并后全套 `uv run pytest` **324 项通过**，`uv run ruff check` 通过，`git diff --check` 通过，`uv.lock` 同步更新。
- 新增 `tests/test_diag_device_contract.py` 的 16 项测试固定：`GET /diag/device` 路由、`X-Internal-Token` + `X-Request-Timestamp` 自校验、401 拒绝、`R<T>` 响应、`iot_charging_device` 十字段快照、`device_id`/`device_code` 二选一必填、`SAFE_VALUE` 注入拦截在查询前执行、`tenant_id` 可空过滤和 `LIMIT 1` 有界查询；并补强两种 SQL 字段/占位符一致性、未命中 `data=null`、空白查询键与租户键在校验顺序前归一、`tenant_id` 不安全值查询前拒绝、查询 SQL 分支选择、审计单对象/数组行数。
- 复查新增的审计行数契约测试初跑失败，暴露 `DiagQueryAuditAspect.rowsOf` 对 `/diag/device` 返回的单个对象记为 `rows=0`；已修复为数组按元素计数、非空对象按 1 行计数。该结论属于源码契约失败与修复，不代表真实生产故障。
- Java 侧行为对应 `docs/diag-query-api-plan.md` §6.6：按 id 或 device_code 单个查询、`tenant_id` 为可选跨租户过滤、未命中时 `data` 为 `null`。
- 未完成业务验收：Java 工件未编译、未部署、未对真实 `cloud-charging-pile-web` 执行 §11 的 Gherkin 场景；不把源码契约测试写成真实故障结论。

## Phase 3a 收口验证（2026-08-27）

对应 issue #43 / PR-D 的 Phase 3a，验证范围是仓库内配置模板、离线数据源路由与文档一致性，不把未接触的生产主机状态写成已执行结果：

- 新增 `tests/test_env_example.py` 的 Phase 3a 模板断言，固定 `.env.example` 不含活动 `AIOPS_MYSQL_*`、`AIOPS_REDIS_*`、`AIOPS_SSH_MYSQL_*`、`AIOPS_SSH_REDIS_*` 配置，同时仍保留 `AIOPS_HTTP_*` 和 `AIOPS_TDENGINE_*` / `AIOPS_SSH_TDENGINE_*`。
- `HybridSources.doctor()` 的 MySQL / Redis 段改为“已收口”，测试固定其 `ok=true, status=deprecated`，即 `http=ok tdengine=ok mysql=deprecated redis=deprecated` 的非阻断语义；`live_sources()` 保持默认 `HybridSources`，`direct_sources()` 仅保留为回退路径。
- 新增 SSH 隧道回归测试，固定 `live_sources()` 只建立 TDengine 转发、`direct_sources()` 仍保留 MySQL / TDengine / Redis 三条回滚转发，与 Phase 3a 收紧后的 `permitopen` 策略一致。
- 自动化检查：任务约定命令 `uv sync --extra dev && uv run pytest && uv run ruff check` 直接通过；`uv run pytest` 全套 **327 项通过**，`uv run ruff check` 通过，`git diff --check` 通过。
- 未完成业务验收：本里程碑未连接真实 `/diag/*` 服务、未在多 provider 服务端执行 `aiops-gateway` fixture 端到端冒烟，也未直接删除生产主机上的 `production.env`；生产凭据删除前的备份和实际删除只能在备好仓库外备份路径与生产访问权限后执行。

## 业务验收待办

生产业务验收仍需要每条支持路径至少三笔由工程师确认结论的真实故障：

1. YKC 金额或电量不一致。
2. 订单结束后缺少交易数据。
3. 状态 2 不可控异常。
4. 状态 5 协议上报异常结束。
5. OCPP 服务端计费。
6. Redis 下游同步问题。

每个案例都要比较生成的摘要、分类、证据和下一步建议与工程师最终结论。即使建议碰巧正确，错误的高置信度仍然算失败。

## AFK 模板 1.1.1 可信交付验证

本次变更把 AFK 的 PR 自动化拆为当前 `main` 的 controller、只读候选 Docker 沙箱和干净 delivery checkout。仓库内 `node .sandcastle/policy-check.mjs workflows` 静态锁定 owner-only 同仓库 gate、controller 执行、候选 token 边界、bundle 交付和 AGENT_PAT fail-closed；afk-bootstrap 的 `test/trusted-pr-delivery.sh` 动态覆盖 stale main、提交保留和远端竞态拒绝。

AFK-B10/B11 已记录在本分支 `qa-plan.md`：提交
`7380fe8c12c738c4f365db8b13c758d204ebed90`（2026-08-30T03:31:15+08:00，
Linux x86_64，Python 3.13.13、Node v24.15.0、actionlint 1.7.12、
ShellCheck 0.11.0），policy checker、actionlint、ShellCheck、pytest、ruff、
compileall、依赖检查和模板 bundle 回归均通过。合并后仍须在在线 self-hosted
runner 上执行 owner-authored `agent:review` canary，保留 workflow URL，并确认
没有 `agent:blocked`；该验证只覆盖开发交付边界，不改变或证明 AI-Ops 业务诊断准确率。

## AFK 模板 1.1.2 工程经济契约验证（2026-08-31）

本次验证范围是项目挂载进 Sandcastle 容器的 AFK 标准与 prompt，不涉及业务运行时：

- `.sandcastle/CODING_STANDARDS.md` 包含同一份 Economy ladder，并明确根因修复、
  现有代码/标准库/平台/已装依赖/成熟依赖/最小自研的选择顺序。
- 单 issue、PRD sub-issue、planner、PR 反馈修复和双轴 review 路径均引用该契约；
  Standards 轴会检查不必要的兼容层、配置、依赖、抽象和 seam。
- 自动化检查：`uv sync --extra dev`、全套 `uv run pytest` **327 项通过**、
  `uv run ruff check`、`uv run ruff format --check .`、`uv lock --check`、
  `uv pip check`、`node .sandcastle/policy-check.mjs all`、
  `node --check .sandcastle/review/review.ts` 和 `git diff --check` 全部通过。
- Dockerfile、镜像工具链和 provider 配置没有变化；规则由项目 worktree 挂载进入
  容器，因此本次不重建镜像。

未完成业务验收：没有连接真实 `/diag/*` 服务、生产数据源或真实故障案例；本节只证明
AFK 治理契约已部署并通过确定性检查，不代表诊断准确率或生产安全边界获得新的验收。

## T1 权限上下文解析验证（2026-08-31）

本次验证范围是 issue #71 的 `ScopeContext` 与身份映射，输入为模拟 UPMS 响应和
平台凭证，不连接任何生产服务：

- `tests/test_scope_context.py`（29 项）用内存目录服务固定解析语义：有效调用者
  解析、目标主体与调用者分离、B 端/C 端 ID 显式映射、未知主体与映射歧义 fail
  closed、越权代查在目标查询之前被拒（不产生后续解析调用）、普通调用者租户越权
  拒绝、平台管理员角色显式切换租户、空业务数据范围 fail closed、角色继承展开、
  `ScopeContext` 不可变、范围指纹确定且防篡改、审计摘要不含凭证与权限副本。
- `tests/test_scope_context_http.py`（12 项）用假传输层守护 UPMS HTTP 契约：
  凭证 Bearer 透传、四类端点固定、数据范围类型归一化、UPMS 不可达/HTTP 401/
  拒绝响应/畸形响应分别映射到 fail closed 错误码、用户 ID 路径注入拦截、错误
  消息不泄露凭证。
- `tests/test_config.py`、`tests/test_env_example.py` 固定 `AIOPS_UPMS_*` 配置
  解析、边界校验和模板约束（无 UPMS 凭证项）。
- 命令 `uv sync --extra dev && uv run pytest && uv run ruff check`、
  `uv run ruff format --check .`、`git diff --check`、
  `node .sandcastle/policy-check.mjs commit` 全部通过，全套 **371 项通过**。

未完成业务验收：未调用真实 `cloud-upms`，端点路径与响应字段契约以 PRD #23 记录
的能力为依据并由离线契约测试守护；`ScopePolicy` 的平台真实代查权限码与管理角色
码未配置，生产接入前代查与租户切换保持关闭；`ScopeContext` 尚未接入诊断运行时
（T2/T3/T4/T5）。真实环境验收按 PRD #23 的验收任务执行，fixture 与模拟响应不能
替代。

## T2 MySQL 受限查询验证（2026-09-01）

本次验证范围是 issue #74 的 ScopeContext → 查询范围下推，输入为模拟 UPMS/Dis
响应与伪造 MySQL 游标，不连接任何生产服务：

- `tests/test_query_scope.py`（17 项）：`QueryScope` 解析（organ/all/self、
  店铺→站点展开、Dis 目标点位交集、self 按用户过滤、空站点短路标记、范围 ID
  超限 fail closed、不可变、`DisHttpDirectory` 令牌与租户头透传、Dis 401/不可达/
  畸形响应 fail closed、路径注入拦截、审计摘要无凭证）。
- `tests/test_mysql_scope.py`（16 项）：订单/费用/占位费/设备查询的租户+站点+用户
  scope 下推、忽略调用方裸 `tenant_id`、空站点短路不发起 SQL、计费模板订单存在性
  检查、占位费订单 `orderId`/`order_no` 二选一、`site_ids_by_shops`/`site_ids_by_points`
  站点归属解析（参数绑定、LIMIT 1000、非法输入拒绝）。
- `tests/test_config.py`、`tests/test_env_example.py` 固定 `AIOPS_DIS_*` 配置解析、
  边界校验、`redacted()` 脱敏与模板约束。
- 命令 `uv sync --extra dev && uv run pytest && uv run ruff check`、
  `uv run ruff format --check .`、`git diff --check`、
  `node .sandcastle/policy-check.mjs commit` 全部通过，全套 **413 项通过**。

未完成业务验收：未调用真实 `cloud-upms`/`dis`，端点契约以 Java `DisFeignClient` 与
PRD #23 记录为依据并由离线测试守护；`QueryScope` 尚未接入诊断运行时（T3/T4/T5）。
真实环境验收按 PRD #23 的验收任务执行，fixture 与模拟响应不能替代。

## T3 TDengine 受限查询验证（2026-09-01）

本次验证范围是 issue #73 的 TDengine 设备集合约束，输入为伪造 `_query` 捕获，
不连接任何生产服务：

- `tests/test_tdengine_scope.py`（7 项）：允许设备查询执行、越权设备拒绝且不发
  起 TDengine 请求（捕获列表为空）、无 allowed 集合时允许任意安全设备、枪属性/
  报文固定超表与固定字段、强制时间窗与 `LIMIT 2000`、设备标识注入拦截。
- `TDengineSource`/`HybridSources`/`live_sources` 的设备集合透传回归覆盖在
  既有 `test_sources.py`、`test_tdengine_scope.py`。
- 命令 `uv sync --extra dev && uv run pytest && uv run ruff check`、
  `uv run ruff format --check .`、`git diff --check`、
  `node .sandcastle/policy-check.mjs commit` 全部通过，全套 **414 项通过**。

未完成业务验收：未连接真实 TDengine，设备集合校验未接入运行时（T5）；真实环境
验收按 PRD #23 的验收任务执行，fixture 与模拟响应不能替代。

## T4 Redis Stream 受限查询验证（2026-09-01）

本次验证范围是 issue #72 的 Redis Stream 租户归属过滤，输入为伪造 Redis 客户端
（记录 XREVRANGE 调用、返回可配置消息），不连接生产服务：

- `tests/test_redis_scope.py`（9 项）：租户命中计数、跨租户消息排除、无租户字段
  消息排除、无 scope 谓词时按订单号匹配、有界读取（`redis_max_messages`）与白名单
  Stream、不返回原始消息正文、非 Stream 类型安全空证据、谓词对 `tenantId`/
  `tenant_id`（str/bytes）的断言。
- 命令 `uv sync --extra dev && uv run pytest && uv run ruff check`、
  `uv run ruff format --check .`、`git diff --check`、
  `node .sandcastle/policy-check.mjs commit` 全部通过，全套 **416 项通过**。

未完成业务验收：未连接真实 Redis，归属谓词未接入运行时（T5）；真实环境验收按
PRD #23 的验收任务执行，fixture 与模拟响应不能替代。

## T5 运行时集成与审计验证（2026-09-01）

本次验证范围是 issue #75 的 ScopeContext 接入诊断运行时，输入为伪造 MySQL/TDengine/
Redis 适配器与离线 fixture，不连接任何生产服务：

- `tests/test_scope_runtime.py`（8 项）：`DeviceGate` 从订单元数据收集允许设备、
  未 seed 为空集合；`ScopedSources` 把 `QueryScope` 下推到 MySQL 并接通 TDengine
  设备集合与 Redis 租户归属谓词；越权设备拒绝且不发请求；`scoped_live_sources`
  构造受 MySQL/ TDengine/ Redis 顶层约束的源；`live_sources` 无 scope 保持既有
  HybridSources 行为；`--scope-json` 解析失败拒绝。
- `acceptance.feature` 新增“基于权限上下文的受限直连诊断运行时”Feature（身份、
  目标主体、租户、受限查询、审计与失败语义 Rule 及 11 个 Scenario）；`qa-plan.md`
  新增 SCP-01..05 可执行 QA 用例。
- 命令 `uv sync --extra dev && uv run pytest && uv run ruff check`、
  `uv run ruff format --check .`、`git diff --check`、
  `node .sandcastle/policy-check.mjs commit` 全部通过，全套 **431 项通过**。

未完成业务验收：未连接真实 UPMS/Dis/生产库；`--scope-json` 的调用者凭证校验属
Java 网关职责，本仓库只消费已解析范围。真实环境验收按 `qa-plan.md` SCP 用例执行，
fixture 与 fake 不能替代。

## 生产 /user/ds 缺陷降级验证（2026-09-01）

本次验证范围是 `UpmsDirectory.data_scope()` 在平台 `/user/ds` 故障时的降级
推导，输入为伪造传输层与真实生产 UPMS（经 124 内网隧道）：

- `tests/test_scope_context_ds_fallback.py`（11 项）：服务错误降级为角色
  `dsType` + `dsScope` + `/shopuser/getShops` 推导（all/本级及子级/自定义/
  多角色取最宽/忽略非本人角色）；凭证失败（401）不进入降级；无角色、未知
  `dsType`、非法响应形状、UPMS 不可达均保持 `scope.upms_unavailable`
  fail closed；`/user/ds` 成功时不触发降级。
- 真实生产验证：`/user/ds` 对 `testadmin` 返回 `系统错误！`（根因为
  `SysOrganMapper.getBizData` 空 IN 子句 + `biz_data` 全空），降级推导得
  `DataScope(type=all)`（角色 `dsType=0`），端到端 `ScopeContext`/`QueryScope`
  正常产出且未扩大平台授予的范围。
- 全套 **442 项通过**；`ruff check`/`format --check`/`compileall`/`pip check`/
  `lock --check`/`policy-check` 全部通过。

未完成业务验收：平台 `/user/ds` 本身修复仍需上游处理；降级路径在组织/店铺
粒度场景仅由契约测试守护。

## 标准调用者认证与订单授权验证（2026-09-01）

本次验证范围是 issue #86 的标准 Bearer token → `ScopeContext` → 订单授权链路，
输入为离线 introspection transport、caller resolver 与订单 authorizer fake，不连接
真实 issuer、introspection 或生产数据库：

- `tests/test_caller_auth.py`：有效 introspection 生成不可变上下文；inactive、错误
  audience、缺 scope、过期、缺主体/租户、非法数据范围均 fail closed；网络失败返回
  可重试错误且不泄露 token/client secret。
- `tests/test_standard_caller_api.py`：有效订单授权、范围外/不存在订单统一 404、缺失
  Bearer、设备 token、非法订单号在查询前拒绝；响应只包含订单号、可访问标记与范围
  指纹。
- `tests/test_gateway_api.py` 回归证明既有 enroll/create/list/get/evidence 与租户拒绝
  行为不受标准认证接缝影响。
- `acceptance.feature` 新增“标准调用者认证与订单授权”Feature；`qa-plan.md` 新增
  SAPI-01..05，覆盖 token、注入、跨主体/租户、introspection 与兼容性。
- 全套 **456 项 pytest 通过**；Ruff、format、compileall、`uv pip check`、
  `uv lock --check`、policy check、`git diff --check` 通过。

未完成业务验收：没有调用真实 access-token issuer/introspection，也没有查询真实订单；
JWT 验签后端尚未实现，需先批准并引入 JOSE 依赖。当前证据只证明 introspection 合同、
失败关闭、范围授权接线和旧接口兼容性，不能代表生产认证或业务准确率验收。

## 最小异步充电健康报告验证（2026-09-02）

本次验证范围是 issue #87 的标准健康报告作业、最小确定性报告和调用者范围隔离，
输入为离线订单、caller resolver/order authorizer fake 与临时 Gateway SQLite：

- `tests/test_health_report.py`：已结束订单生成停止原因指标、确定性摘要、完整度、
  `health-v1` 与数据时间；停止原因缺失逐项 unavailable；订单不存在、充电中、缺设备、
  时间无效和窗口超限使用稳定错误码。
- `tests/test_health_report_jobs.py`：queued/running/completed 复用、scope 隔离、失败后
  重建、启动时遗留作业过期、终态不被迟到更新覆盖。
- `tests/test_health_report_api.py`：Bearer 调用方创建和轮询、202/retry_after 合同、
  越权订单不落库、其他 caller 与随机 job ID 不可读、响应不泄露 scope fingerprint。
- `acceptance.feature` 新增“最小异步充电健康报告”Feature；`qa-plan.md` 新增
  HRJ-01..05，覆盖生命周期、复用、准入、超时/重启和隐私。
- 全套 **471 项 pytest 通过**。新增 deadline 回归证明迟到 completion 被拒绝，作业
  保持 expired 且不保存迟到报告。PR #94 的其余同一代码曾通过 Linux/Windows CI、Workflow
  policy 和 CodeRabbit minimal-risk review；PR #98 的撤销原因是会话治理回滚，不是
  验证失败。本次恢复后重新执行全部确定性检查。

未完成业务验收：没有连接真实 access-token issuer、生产订单、TDengine 或 Redis；
最小报告仅验证接口和状态机，不代表完整健康评估，更不能宣称业务准确率通过。

## 标准单问诊断验证（2026-09-02）

本次验证范围是 issue #88 的标准诊断 API、主体隔离、生命周期和现有 Agent 接线，输入为离线 caller/order fake、临时 SQLite、模拟 AgentDiagnosis，不连接生产服务：

- `tests/test_standard_diagnosis_api.py` 覆盖 202 创建、状态查询、列表范围、随机/跨主体 404、extra 字段拒绝和响应内部字段隔离。
- `tests/test_standard_diagnosis_runtime.py` 覆盖 runtime 创建私有 workspace、调用 `run_agent_diagnosis(scope=...)`、diagnosed→completed、inconclusive 保留与迟到 completion 过期。
- `acceptance.feature` 新增标准单问诊断 Feature；`qa-plan.md` 新增 DX-01..04。
- 诊断 API scope 回归：`aiops:orders:read` 不足以创建诊断，必须具备
  `aiops:diagnoses:write`，缺失时返回 403 `INSUFFICIENT_SCOPE` 且不落库。

未完成业务验收：未连接真实 access-token issuer、生产订单、provider 或真实模型；当前证据只证明标准 API、范围隔离和 Agent 接线，不代表真实故障诊断准确率。
## 标准健康报告曲线验证（2026-09-02）

本次验证范围是 issue #89 的曲线、降采样和来源摘要，输入为离线时序 fake，不连接真实 TDengine：`tests/test_health_curves.py` 覆盖 400 点降采样、数值时间排序、重复时间、首末点、极值、缺失值和不伪造系列；worker 将 telemetry SourceError 转为部分完成、空曲线和 unavailable 来源状态。未完成业务验收，真实字段映射留给 #92。
## 完整充电健康指标验证（2026-09-02）

本次验证范围是 issue #90 的公式边界、SOC/SOH 单位和缺容量语义，输入为离线数据：

- `tests/test_health_metrics.py` 覆盖五类评分边界、SOH 浅充拒绝、物理范围、缺权威容量和 radar unavailable。
- 健康报告 worker 复用完整时序输入计算指标，曲线降采样不参与指标计算。
- `acceptance.feature` 新增完整指标 Feature；`qa-plan.md` 新增 METRIC-01..02。

未完成业务验收：未连接真实 TDengine、车辆档案或协议告警码表；当前证据只证明公式实现。
## 统一标准 API 契约验证（2026-09-02）

本次验证范围是 issue #91 的健康报告/单问诊断统一外部契约，使用离线 TestClient 和 fake runtime：`tests/test_standard_api_contract.py` 验证两类资源共享 Bearer/错误结构、opaque ID、独立状态和不泄露内部字段；未完成真实环境业务验收。
## 标准 API 真实环境验收准备（2026-09-02）

已新增 `qa-plan.md` 的 REAL-API-01..04，覆盖完整订单、部分数据、越权资源、诊断与性能。经环境盘点，当前没有标准 API issuer/introspection 配置、批准调用方 token、部署入口或本 PRD 可用真实订单，因此四项均记录为 blocked；既有 PRD #23 真实验收资料不作为本次通过证据。解除条件和证据格式已写入 QA 计划。
## 标准 API 结构仿真数据验证（2026-09-02）

新增 `examples/synthetic-acceptance/` 和两个 runner：生成器输出完整、部分和越权三类生产结构仿真数据；执行器调用现有 `FixtureSources`、健康报告、曲线和指标逻辑，断言完整订单 400 点降采样为 300 点、部分订单 telemetry unavailable、跨租户订单 ORDER_NOT_FOUND。runner 通过，数据敏感字段扫描无命中。

该结果只证明离线链路和数据契约可运行，不能替代 REAL-API-01..04 的真实 issuer、调用方、生产订单和业务人员验收。
## C 端 thirdSession Redis 适配验证（2026-09-03）

真实 Redis key 前缀为 `app:3rd_session:`，值为 Java 序列化外壳内嵌 JSON，支持 login 指针
到 wx 会话。AI-Ops 只读解析，不执行 Java 反序列化；截图 token
`76ea8a54-12d7-4889-823e-edded054a7218` 的直接键和 login 键均不存在，真实验收未完成。
本地全量 pytest/Ruff 与真实 Redis ACL 连接均通过。阻塞条件是当前有效 C 端会话及其有权订单。

## 公司 cloud-auth Bearer 适配验证（2026-09-02）

新增 `UpmsCallerResolver`：Bearer token 原样交给现有 UPMS 用户/数据范围接口，解析为 ScopeContext；未配置 introspection 时标准 API 不再静默禁用，平台凭证失败仍 fail closed。没有复制 HS256 密钥或信任裸身份字段。当前仅完成离线/平台契约接线，真实 token 和订单验证待服务部署后执行。

## 服务迁移至公司 120 验收（2026-09-06，issue #147）

生产实例迁至公司 120 服务器（root systemd `aiops-gateway.service`，`127.0.0.1:8788`，开机自启），公网入口统一为 `https://api.qumall.qushiyun.com/v1/*`（120 nginx 同机反代 + 服务身份头注入）。原 ranlei 服务器侧网关与 SSH 隧道单元停用（单元文件保留为冷备；FRP 域名保留为回滚入口，不再承载 `/v1` 流量）。

S1 影子验收（120 本机）：健康检查 200；FAQ 推荐 200（28 条）与答案 200；无 Authorization 头返回 401 `INVALID_ACCESS_TOKEN`；真实订单健康报告 202→completed（indicators=[stop_reason]）。
S2 切流验收（公网入口）：FAQ/答案 200；401/422 错误语义透传；真实订单健康报告 completed；单问诊断创建 202；120 实例日志逐条对应本次请求，原服务器 `/v1` 请求归零。
S3 退役验收（公网入口 + 开机自启）：FAQ 200（28 条）、健康报告 completed、诊断历史按主体隔离可见 3 条；单元改名后重启竞态（旧进程占用 8788）已由 systemd `Restart=on-failure` 自动恢复，重启后公网验收通过。

遗留：单问诊断终态复验受模型供应商配额限制（glm-ark 月配额 2026-09-21 重置；psydo key 池停用），诊断执行面已通过（202 + Agent 管线事件完整）。2026-09-04/05 记录的 ranlei 域名与 FRP 链路为迁移前历史状态。

## T2 智能体生命周期验证（issue #169，2026-09-09）

验证范围：AI-Ops `AgentStore`/`AgentManager` 草稿、发布、停用、删除与版本快照协议，以及管理 API 的 Bearer scope/角色接缝；不包含真实 UPMS、Java BFF、后台浏览器页面或生产知识库状态。

- 自动化测试：`tests/test_agent_lifecycle.py` 4 项通过。
- 覆盖行为：租户隔离与统一未找到、角色权限、草稿 revision 乐观并发、模型与知识库发布校验、不可变 version snapshot、发布版本派生新草稿、停用保留历史版本、已发布智能体不可删除、管理 API 生命周期响应。
- 全套回归：pytest 554 项通过（T1 合并后）；Ruff、格式、compileall、`uv lock --check`、`uv pip check`、`git diff --check` 通过。
- 结果：生命周期协议验证通过；草稿允许保存待审核模型，发布时才执行后端 allowlist；默认未注入真实知识库 resolver 时对有绑定的发布请求 fail closed。

未完成业务验收：当前使用临时 SQLite、fake resolver 和 fake caller，不能宣称真实后台角色、知识库解析状态或 Java BFF 链路已验收。

## T1/T2 合并后 AI-Ops 本地集成验收（2026-09-10）

- 环境：本地 Python 3.13、FastAPI `TestClient`、临时 SQLite、fake caller、fake 知识库和内存媒体对象。
- 专项结果：T1 媒体协议、T2 智能体生命周期、统一问答 API、QA store、Gateway API 共 31 项通过。
- 全量结果：pytest 554 项通过；Ruff、格式、compileall、`uv lock --check`、`uv pip check`、`git diff --check` 通过。
- 已验证：媒体授权/Range、租户隔离、草稿发布版本、停用/删除边界、统一问答既有入口回归。

真实端到端验收仍未完成：#170 尚未把受限检索接入 QA 运行时，当前工作区也没有 Java BFF、`qumall-admin` 浏览器环境、真实 `kb-service/RAGFlow` 和可批准的图片/视频知识库数据；本地 fake 集成结果不能替代真实媒体验收。

## T3 客服 QA RAG 运行时验证（issue #170，2026-09-10）

验证范围：AI-Ops 统一问答入口 `qa` 路径按已发布客服智能体运行的 harness 接线与 blocks-v1 合同；不包含生产 `kb-service`、RAGFlow、Java BFF 透传改造或浏览器渲染。

- 自动化测试：`tests/test_qa_rag.py` 14 项通过（blocks 合同、媒体授权、引用校验、状态三态、补检索、调用上限、寒暄免检索、智能体选择边界）。
- 全量回归：pytest 568 项通过（554 基线 + 14 新增）；Ruff、格式、compileall、`uv lock --check`、`uv pip check`、`git diff --check` 通过。
- 已验证：仅已发布客服智能体服务 qa 作业（草稿/运维/停用/跨租户不选中）；模型漏检索业务问题时 harness 强制一次补检索（总上限两次）；image/video/reference 块只能引用本轮检索签发的资源与分段，伪造即丢弃；`found` 无依据被降级 `not_found`；kb 不可用降级 `unavailable` 且文本保留；未配置 kb-service 或无已发布智能体时回退既有零订单回答路径。
- 配置接缝：`AIOPS_GATEWAY_KB_SERVICE_BASE_URL`/`AIOPS_GATEWAY_MEDIA_SIGNING_SECRET` 注入运行时（`KbServiceClient.for_tenant` 按请求租户重绑定）；两者留空即维持旧行为。

未完成业务验收：mock-first 协议级验证不等于真实媒体链路验收；生产 kb-service/RAGFlow canary（当前停机，见 docs/agents/kb-service-test-env.md）、Java BFF blocks 透传与前端渲染由 #171/#173 及 P0-E2E-REAL 覆盖。

## T4 会话与活跃订单上下文验证（issue #172，2026-09-10）

验证范围：会话 CRUD、scope/入口/智能体版本绑定隔离、8 轮/8k token 双上限上下文窗口、30 天保留、活跃订单每轮重校验与分流、409 CONVERSATION_BUSY、取消轮次不留答案；不包含真实多设备前端续聊与 BFF 透传（#173/P0-E2E-REAL）。

- 自动化测试：`tests/test_conversation_api.py` 10 项通过。
- 全量回归：pytest 578 项通过（568 基线 + 10 新增）；Ruff、格式、compileall、`uv lock --check`、`uv pip check`、`git diff --check` 通过。
- 已验证：跨租户身份层拒绝、跨入口统一 404（含 GET/DELETE/active-order 端点）；绑定活跃订单必须归属校验（未授权统一 404）；follow-up 省略订单号复用活跃订单走 diagnosis，归属撤销后清除绑定走 qa；知识问题始终 qa+RAG；并发 409 且无会话提问不受影响；崩溃锁 120s 自过期；取消/无答案轮次不保留、不进上下文；窗口取 8 轮与 8k 较小者（单条超预算轮次单独保留不返回空上下文）；30 天过期不可见。
- 版本语义：会话记录 agent_version_key；`select_customer_agent` 每回合现查最新已发布版本（新回合新版本），执行中回合持 #170 的 selection 快照（原版本），双向满足 #172 版本切换验收。

未完成业务验收：前端刷新/跨设备续聊的真实轮询体验、BFF 会话字段透传、真实"停止生成"按钮链路属 #173；未连接生产环境，不把 fake 结果记为业务验收。
## T1 受限知识检索与媒体资源协议验证（issue #168，2026-09-09）

验证范围：AI-Ops 内部 `knowledge_search` guard、RAGFlow 字段规范化和媒体资源授权协议；不包含生产 `kb-service`、RAGFlow、Java BFF 或浏览器部署。

- 自动化测试：`tests/test_knowledge_retrieval.py` 4 项通过。
- 回归测试：assistant API、assistant QA store、Agent contracts、diagnostic tools 共 29 项通过。
- 覆盖行为：租户/智能体版本知识库白名单、`top_k` 隐藏、每轮两次检索上限、图片/视频 MIME 白名单、TTL、主动失效、跨租户拒绝、Range 206、依赖不可用降级。
- 结果：协议和安全边界验证通过；测试环境为本地 Python 3.13、内存 fake、时间 `2026-09-09`；源提交 `efcaf29` 已通过 PR #176 squash 合并为 `c8be858`。

未完成业务验收：没有连接真实知识库或实际前端，不能把本次 fake/协议测试写成图片视频业务链路已验收。

## T5 管理与草稿调试验证（issue #171，2026-09-10）

本次分支 `feat/agent-debug-run`，接口级口径（范围决定见 qa-plan.md ADMIN 节）。

- 单元/协议测试：`tests/test_agent_debug.py` 11 项通过。覆盖 KbBindingResolver 四态（DONE 放行、RUNNING/UNSTART 拒绝含"解析中"、FAIL 拒绝含"解析失败"、kb 详情 502 拒绝含"不存在"、kb 不可达 fail closed 含"不可用"、空绑定零调用、发布租户重绑）、`run_agent_debug_answer`（blocks 预览 + draft 版本标记 + 授权图片块）、`POST /v1/agents/{id}/debug-run`（admin 200 / viewer 403 / 跨租户 404 / 非草稿 409）。
- 契约依据：kb-service GET 面按 `~/Playground/experiments/kb-service-design/app.py` 与 RAGFlow v0.27.1 源码核实——`GET /kb/knowledge-bases/{kb}`（`accessible()` 租户判定，越权/不存在统一 502 upstream_error）、`GET /kb/knowledge-bases/{kb}/documents`（`docs[].run` 为 TaskStatus 名 UNSTART/RUNNING/CANCEL/DONE/FAIL/SCHEDULE）。
- 回归：全量 pytest 589 项通过（578 基线）；Ruff、ruff format、compileall 通过。
- 行为变化声明：`create_gateway_app` 默认 AgentManager 在配置 `AIOPS_GATEWAY_KB_SERVICE_BASE_URL` 时从 fail-closed `UnavailableKnowledgeBindingResolver` 切换为 `KbBindingResolver`——此前该配置下发布必被拒，本票起为真实校验；未配置 kb URL 的部署保持 fail-closed 不变。

未完成业务验收：ADMIN-REAL 真实 kb-service canary BLOCKED（120 KB 栈 2026-09-09 起停机，重启为人工决策点）；qumall-admin 浏览器页面验收范围外。fake GET client 结果不替代真实发布校验验收。

## T7 监控与脱敏审计验证（issue #174，2026-09-10）

本次分支 `feat/agent-metrics`。

- 单元/协议测试：`tests/test_agent_metrics.py` 12 项通过。覆盖 MetricsStore 聚合回读（runs/completed/failed/busy/tokens/media、by_route/by_retrieval/by_error 分桶、agent 过滤、租户互不可见）、枚举与字段严格校验（未知 route/outcome/retrieval、空租户、小写错误码、负计数全拒绝）、脱敏字段集断言（行字段白名单，无 question/answer/prompt/media URL）、30 天写时清理、API 面（admin/viewer 200、无角色 403、跨租户隔离、未认证 401、非法过滤 422）。
- 真实运行时集成：真实 GatewayRuntime + 共用 SQLite，monkeypatch 模型抛错 → 作业 failed 且指标行 (qa, failed, QA_FAILED, duration) 落库，行内无问题原文（METRICS-06）。
- 失败码覆盖映射：QA_FAILED（qa 模型/检索失败）、DIAGNOSIS_FAILED/DIAGNOSIS_BLOCKED（诊断失败/受阻，inconclusive 以 DIAGNOSIS_INCONCLUSIVE 计入失败桶）、CONVERSATION_BUSY（会话忙碌）、KB_UNAVAILABLE（调试时 kb 不可达）；检索 not_found/unavailable 由 retrieval_status 维度区分。
- 回归：全量 pytest 601 项通过（589 基线）；Ruff、ruff format、compileall 通过。
- 行为变化声明：`_try_customer_rag` 在完成结果上追加 `agent_version` 标签用于指标归因，但落库与对外的 blocks 合同保持不变（存储时剥离该标签）；新增端点为只读查询，无破坏性变更。

未完成业务验收：METRICS-REAL 生产流量监控验收 BLOCKED（待 #173 真实 canary 与 KB 栈恢复）；监控页面属范围外（接口级口径）。
