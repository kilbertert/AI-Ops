# 仓库目录迁移

## 2026-10-29：退役验收后开始

Python Thin Harness和诊断/设备CLI已完整删除；独立aiops-admin保留。
C端与管家原H5、Dify原生预览及独立CPMS/跨主体拒绝已通过，证据见
[退役真实验收](../validation/2026-10-29-runtime-retirement-live.md)。历史故障准确率不在此结论内。

第一片将现役Java节点16个文件从`tools/m2_close/node/`整体迁至`services/execution-node/`，
同步所有现役源码/构建/实验引用；原Java文件逐字不变，宿主安装目录不变。
隔离驱动继续在tools，Gateway镜像不再包含节点源码。验证见
[第一片回执](../validation/2026-10-29-execution-node-layout.md)。

后续目标：Python按consumer/knowledge/identity/publication/gateway职责形成包，测试随职责归类；
运行服务与一次性工具分开，部署面按服务聚合。每片更新调用者、资源加载、打包、CI与现役文档，
保留错误处理与权限校验，不新建转发空壳或永久legacy目录。

## 2026-10-28调查归档（不再作为保留或执行决定）

以下是退役范围扩大前的原调查，数量与保留建议只属于当时。旧诊断消费者迁移步骤已被完整退役取代，
不得再按其恢复旧能力；以下旧路径用于解释历史问题。

# 仓库目录梳理与 Thin Harness 退役清单

核查基线：`55a284d6`，2026-10-28。本文区分本轮实际删除和后续迁移建议；目标目录尚未整体落地。
范围已确认：删除仓内无生产调用的旧代码，仍在用的部分列出迁移清单，不下线现役诊断。
当前功能与部署结论见[系统架构](../system-architecture.md)及[交付状态](../agents/current-delivery-state.md)。

## 一、问题在职责混杂，不在目录深度本身

按 `git ls-files -z` 统计跟踪文件，不含虚拟环境、构建产物和本机材料：

| 位置 | 基线规模 | 具体问题 |
|---|---|---|
| `src/aiops_diagnostics/` | 134 个文件，129 个 Python 模块全部位于包根 | Gateway、业务、Dify、旧推理与基础能力没有目录边界 |
| `gateway_api.py` | 8,500 行，静态导入 67 个本包模块 | 同一工厂装配身份、业务路由、控制台、内部回调与文档 |
| `gateway_runtime.py` | 3,867 行，静态导入 46 个本包模块 | 诊断、问答、知识、分析与异步作业执行聚在一起 |
| `tests/` | 248 个文件，其中 244 个直接位于根层 | 功能测试难以随对应模块定位，部分测试硬编码目录深度 |
| `tools/` | 123 个文件，29 个直接位于根层 | 生成器、探针、实验和现役 Java 节点源码混放 |
| `deploy/`、`ops/` | 分别 78、35 个文件 | 部署制品、代理源码、环境声明和运维步骤交错 |
| `docs/` | 479 个文件，27 个直接位于根层 | 已有分类；主要问题是当前说明与历史证据的适用范围 |

导入数量按 AST 扫描，包括函数内部延迟导入，只计不同的本包模块；它不是线上调用量。
已有 `agent_lifecycle`、`shortcut_lifecycle`、`conversation_store` 等职责可以直接沿用，
无需再造一套 controller/service/repository 基类，也不新增泛化 `utils/` 收容混杂代码。

## 二、Thin Harness 当前仍是一条可达的执行链

```text
CLI diagnose / agent-resume
设备 /v1/runs
标准 /v1/standard/diagnoses
助手的订单检测分支
Dify 旧诊断试用
  → agent_runner.run_agent_diagnosis
  → agent_engine.AgentCoordinator
  → codex_runtime.SDKCodexSession
  → diagnostic_tools.DiagnosticToolExecutor
  → sources（有界 SQL / HTTP / TDengine / Redis）
  → journal.EvidenceJournal + agent_validator.AgentResultValidator
  → 工作区 / 作业记录 / 诊断响应
```

依据：`cli.py`、`gateway_runtime.py`、`dify_trial.py` 的调用，及 `agent_runner.py`
对协调器、工具、证据日志的装配。助手入口在 `gateway_api.py` 中仍调用
`start_standard_diagnosis`；不能仅因 WF01 和 Java 已存在就认定所有旧消费者已经迁移。

### 本轮实际删除

| 删除项 | 核查结果 | 调用方处理 |
|---|---|---|
| `config.DiagApiSettings` | 已标记 deprecated 的类型别名；跟踪代码无引用 | 现役类型为 `HttpSettings` |
| `Settings.diag_api` | 生产代码无调用，仅测试使用；实际 HTTP 数据源读 `Settings.http` | 测试统一使用 `Settings.http` |
| `AgentSettings.provider_names()` | 只将 `providers` 投影为名称；仅一个测试使用 | 该测试直接核对已解析注册表，保留顺序断言 |

这里的“无调用”指仓内跟踪代码、脚本、入口和配置检索；不宣称扫描过所有外部 Python 消费者。
旧环境变量 `AIOPS_DIAG_API_*` 的回退读取、`redacted()` 的 `diag_api` 字段仍属配置/输出兼容，
有测试覆盖，未借此次删除 Python 别名改变它们。HTTP 令牌校验、凭据脱敏、查询上限均保留。

### 仍在用的迁移清单

| 模块或链路 | 当前依赖与删除障碍 | 移除前必须完成 |
|---|---|---|
| `agent_engine`、`diagnostic_tools`、`engine` | CLI、Gateway 诊断、Dify 试用；确定性引擎还作为 advisory runbook 工具 | 明确每个入口迁往何处或退役，验新链，再删除旧调用与对应专属测试 |
| `agent_runner` | 除诊断外还提供零订单答复和模型分类 | 先将仍需的问答/分类从诊断执行中分离，不能整文件删除 |
| `codex_runtime`、`codex_launcher`、`responses_adapter` | QA、分析叙述、旧诊断共享 SDK；适配器有 CLI 和部署手册入口 | 消费者迁完后核实际服务流量与配置，再移除 SDK、脚本入口和专属适配器 |
| `agent_workspace`、`journal` | QA、分析、试用、诊断与历史证据回读 | 明确存量任务终态、历史结果读取和证据保留；不可顺手删除数据目录 |
| `agent_contracts` | 生命周期、Dify 工具目录、发布/配置、QA 块结构共用 | 将现役契约归还对应模块，再删除诊断专属合同；保留旧快照的读取判据 |
| `agent_validator` | 新 `operator_cpms.validate_body` 仍导入 `EXECUTED_MUTATION` | 先迁移共享的禁用动作判据并保留测试，再移除旧诊断验证器 |
| `sources`、`query_scope`、`order_visibility` | 身份解析、管家授权、只读分析和订单可见性共用 | 拆出真实数据访问与共享权限规则；不能随诊断引擎一起删除 |
| `ops_tools`、`ops_provider`、Dify 旧工具 | 与新 CPMS 工具共存；发布快照仍可引用旧工具名 | 查发布配置、实际调用及恢复版本；迁发布记录后移除旧 provider/端点 |
| 设备入口、`gateway_client`、便携打包 | CLI 仍注册设备命令；Windows 分发退役不等于本地打包入口退役 | 单独确认设备消费者、历史 run 回读和本地打包用途，更新命令与交付合同 |
| SOP、fixture、`docs/architecture.md` | 工作区、wheel、PyInstaller 与 CD 会携带/读取这些文件 | 先解除运行时引用，再处理文件；历史验收与 ADR 不按死代码删除 |

完整退役的完成条件：旧写入/启动入口无消费者，替代路径验收通过，旧运行不再产生，
存量结果有明确读取方案；专属源码、配置、依赖、测试、脚本、部署资产和当前文档同步清除。
生产适配器或入口关闭还需实际使用观测。本轮只核仓库，不执行生产流量切换或服务退役。

## 三、建议的目标目录

保留现有 Python 分发包名和顶层标准构建入口，以职责归类，不为整齐重命名所有文件。
下面是迁移目标，不能当成当前存在的导入路径：

```text
src/aiops_diagnostics/
  gateway/       # HTTP 装配、路由、配置、网关存储和作业调度
  identity/      # 公司登录、调用者范围、订单授权、委派凭据
  consumer/      # WF01、订单/账务问答、语言、图片输入、试用发布
  operator/      # 管家 CPMS、运维聚合与说明
  knowledge/     # 检索、媒体、租户知识库配置与同步
  publication/   # Agent/快捷动作生命周期、内容与路由绑定
  conversation/  # 会话存储、上下文、FAQ/宣传选择和澄清
  dify/          # 登录、workspace、控制台发布、DSL 和执行适配
  execution/     # Java 委派合同、任务账本、节点发布、执行配置
  diagnosis/     # 尚未退役的 Python 诊断，实现迁完后删除
  model/         # 尚被 QA/分析/诊断共同使用的模型会话与解析
  cli/           # 现有三个 CLI 模块与命令装配
  common/        # 已有 HTTP、有界文件、脱敏、语言及路径原语
services/
  execution-node/ # 现役 Java Maven 工程
  model-proxy/    # 节点仍使用的模型代理
integrations/
  cpms-java/      # 公司 Java 侧集成补丁，独立于执行节点
plugins/         # Dify 插件保持原生打包布局
deploy/          # 按 gateway / node-runtime / dify / knowledge 归属部署资产
ops/             # 运行手册、环境声明；部署动作引用 deploy 的唯一实现
tools/           # 内容生成/迁移工具、验证探针和隔离实验
tests/           # 按职责镜像子目录，跨模块 HTTP/部署验证独立放 integration/
docs/            # 架构、ADR、运行手册、规格、历史证据沿用已有分类
```

`common/` 只容纳已有的通用原语，不放业务合同、数据库表、发布规则或 Gateway 编排。
`config.py` 暂时保留装配职责；无需为了移动目录拆成多个互相依赖的配置文件。

| 现位置 | 建议归属与拆分重点 |
|---|---|
| `gateway_api.py` | `gateway/app.py` 保留装配；路由按 consumer/operator/console/internal 分组，先保持依赖注入与鉴权语义 |
| `gateway_runtime.py` | 作业调度留 `gateway/`；各业务执行分别归 consumer/operator/diagnosis/conversation |
| `caller_auth`、`company_*`、`third_session_auth`、`scope_context`、`query_scope`、`order_visibility` | `identity/`；规则只保留一份，不能按 consumer/operator 复制授权逻辑 |
| `consumer_*` | `consumer/`；JSON 与使用它的语言模块同迁 |
| `operator_cpms`、`ops_analysis*` | `operator/`；旧 `ops_tools` 与新 CPMS 的专属/共用部分先分清 |
| `knowledge_*`、`kb_onboarding`、`ragflow_console` | `knowledge/`；Dify 专有协议适配仍归 `dify/` |
| `agent_lifecycle`、`shortcut_lifecycle`、`agent_content`、`agent_manifest`、`chat_route_bindings` | `publication/`；这些是现役编辑/发布能力，不因名称含 agent 而进入待退役目录 |
| `agent_runs`、`execution_manifest`、`chat_execution`、`node_publication` | `execution/`；名称相似也不能与 `agent_engine` 混为旧执行器 |
| `codex_runtime`、`codex_launcher`、`responses_adapter`、`turn_recovery` | `model/`；先拆共享会话依赖，最终是否删除由消费者决定 |
| `agent_contracts`、`agent_workspace`、`sources`、`agent_runner` | 跨职责文件，必须先拆具体使用部分，不能整文件机械归类 |
| `tools/m2_close/node/` | `services/execution-node/`；Maven 源码和自身测试一起迁，实验驱动留 tools |
| `tools/m2_live/model_proxy.py` | `services/model-proxy/`；现役 compose 启动它，不是一次性探针 |
| `tools/m2_close/` 剩余内容、`tools/m2_live/` 探针 | `tools/verification/` 或 `tools/experiments/`，按实际启动者区分；不按里程碑过时推定废弃 |
| `java/cloud-charging-pile-web/` | `integrations/cpms-java/`；这是公司侧补丁，不是本仓现役节点 |
| `ops/dify-*` 与 `deploy/dify-36/` | 按 Dify 组件聚合同一部署面；脚本、JS/HTML、补丁原文、配置模板一起核对引用 |

## 四、迁移顺序与每片验收

1. **本片：核实依赖、删除无消费者的旧兼容代码。** 列出保留原因与目标归属，不改变公开路由。
2. **归位现役执行节点与代理。** 同片更新 Maven 路径、compose 镜像内路径、镜像构建上下文、
   内容锁、CI、`.gitignore` 和运行手册；确认启动命令与 jar/代理位置一致。代码路径移动不等于部署目录迁移。
3. **分块形成 Python 包与对应测试目录。** 优先职责完整的 consumer/knowledge/identity，
   核对导入图；再拆 Gateway 装配和大文件。每片只做一个职责的移动，不同时改业务语义。
4. **逐个迁移旧诊断消费者。** 分别验 C端检测、CLI、设备、标准诊断、Dify 试用、QA、分析；
   对无替代的能力明确保留或另行退役。新 Java 两工具不等价于旧诊断全部证据能力。
5. **删除最后的专属链路。** 入口、模型依赖、配置、契约、脚本与部署资产按实际剩余引用清理，
   不保留空壳转发、注释掉的旧实现或 `legacy/` 永久副本。历史验收留档。

目录移动的影响面必须覆盖：Python 绝对/延迟导入、字符串 monkeypatch、`python -m` 和
`project.scripts`、相邻 JSON/HTML/YAML、wheel `force-include`、PyInstaller、Docker COPY、
compose、脚本工作目录、测试的 `Path(__file__).parents[...]`、README、`.spec` 与仓库地图生成器。
目前 README 测试用 `PACKAGE.glob("*.py")`，引入子包时必须同步改成递归识别完整模块名，
否则“通过”只代表没扫描到新目录。不要删掉该检查来让移动过关。

每片运行 ruff、相关测试、全量回归及 wheel/sdist；Java 移动还跑 Maven 验证和隔离实验。
改变 Gateway 外部行为时必须补[一次性 Gateway 验证](../../.claude/skills/verify-aiops-gateway/SKILL.md)。
这些本地检查不证明真实诊断、Dify/Java 整链、知识库活性或生产业务准确率。

本轮未把目录迁移当作已经实施的架构决定；后续按实际依赖、兼容范围和独立 PR 更新此表。
