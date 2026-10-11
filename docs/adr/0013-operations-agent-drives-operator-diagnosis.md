# 管家端单订单诊断由已发布的 operations 版本驱动

日期：2026-10-12
状态：已采纳
影响版本：PRD #693 的子票 #695

## 背景

`agent_type="operations"` 这个槽位在代码里**一直存在**：登记表允许它
（`agent_lifecycle.AGENT_TYPES`），发布门强制它必须配 `diagnosis-v1` 契约
（`agent_lifecycle._validate_config`），清单里也给了它默认契约
（`agent_manifest.DEFAULT_OUTPUT_CONTRACTS`）。但运行时**没有任何一处读它**——
唯一读过它的地方是 `qa_rag.select_customer_agent`，而那里是把它**跳过**。

于是管家端（`X-Business-Entry: operator`）对一张已授权订单发起诊断时，跑的是烤死在
`agent_engine._initial_prompt` 里的提示词。运营想改一句运维口径，必须走一次代码发布。
这就是 #693 的 ② 号路径要拆掉的东西。

同时期已经存在的机制有两个可复用件：**运行时登记表**（`dify_app_registry`，把
`(租户, 入口)` 映到一个 agent 名，`#584`）与**注册表驱动的选版**（`gateway_runtime._registered_agent`
+ `qa_rag.select_customer_agent`，问答路径已经接线）。本决策把诊断路径接到同一条机制上。

## 决定

**五点。**

### 一、入口由**原始头**承载，并透传到工作线程

`/v1/standard/diagnoses` 此前读 `X-Business-Entry` 只用于鉴权与数据范围，读完即丢弃。
现在把它作为 `business_entry` 透传到 `start_standard_diagnosis` → `_execute_standard_diagnosis`；
三个助手子路由（route 1 / 1b / 1c）把它们的 `decision.platform` 一并传下。

判据用 `scope_context.is_operator_entry`（`strip().lower() == "operator"`，未知/缺失 ⇒ 最窄），
**不**在诊断路由上另起一次平台身份解析：入口在这里要回答的是「哪份运维配方服务它」，
与 `third_session_auth` 用同一个头回答的是同一个问题；而数据范围仍来自会话，
路由不因为多解析一次平台身份而多拿到任何授权。

### 二、四个调用点同源，消费者侧**不是**特例

入口是**每个调用点各自的上下文**，不通过 `ScopeContext` 传递（该类型的指纹会把入口
当成新维度，从而改变既有的会话/作业归属）。因此断言的形状是：**四个调用点都要把它
传到位**，且消费者侧的值是 `"consumer"` 而不是空——一个「只有 operator 才传」的实现
会让两侧的隐身差异长期无人发现。

### 三、选版是**查找**，不是搜索；未采用登记表时保持旧行为

新增 `ops_agents.select_operations_agent(store, tenant_id, agent_name)`：按登记表点名的
**名字**取那份已发布的 `operations` 版本。名字不符、未发布、类型不符、提示词为空——
四种都返回 `None`。

`None`（登记表没给名字）**不**回退到「最新已发布」。这与问答路径的
`select_customer_agent(agent_name=None)` 刻意不同：那里的兜底是「另一个库」，这里的
兜底是**烤死提示词**，而烤死提示词正是本决策要拆掉的东西。

未采用登记表的环境（表里**一行都没有**）保持 #695 之前的行为：不注入、跑烤死提示词。
这与问答路径同向（`_registered_agent` 在未配置时返回旧规则），也是「这份门禁可以
安全地空转上线」的依据：**第一行登记表出现之前，谁的行为都不变**。

### 四、fail-closed 的是「已采用但这一行服务不了」

登记表已采用（存在任意行）而 `(租户, operator)` 这一行选不出服务得了的版本时，
作业以 `DIAGNOSIS_AGENT_UNREGISTERED` **终态失败**，不触模型、不回退烤死提示词。
四种成因（未登记该行 / 名字不存在 / 类型是 customer / 只有草稿）**同形**处置：它们
对调用方的含义完全相同——这个入口不被服务——而区分它们是一句关于配置的话。

使用 `operator` 头**不构成**一条单独可利用的路径：它换来的只是「按哪份配方作答」，
身份与数据范围仍来自会话（`#448` 的老债与本决策无关，也未被它掩盖）。

### 五、「护栏不可被覆盖」靠的是工具层与校验器，不是提示词顺序

注入点选在 `AgentCoordinator._initial_prompt`，作为「运维方法论」块放在**工具列表之前**，
使语言规则、标识符规则与收尾的 schema 句都排在它之后。但顺序只是形状：

| 护栏 | 住在哪 | 能否被 Dify 的提示词覆盖 |
|---|---|---|
| 只读边界 | 工具层（`DiagnosticToolExecutor` 只暴露枚举工具） | **否**，是机制不是措辞 |
| 证据纪律 | `AgentResultValidator`（证据引用、置信度上限、写操作正则、密钥扫描） | **否**，确定性校验 |
| 输出语言 | `answer_chinese_leak`（诊断定稿点） | **否**，定稿后判定 |
| 结构契约 | `AgentDiagnosis` / `AgentTurn` 严格 schema | **否**，Pydantic |
| 「运维方法论不得与护栏冲突」 | —— | **无人强制**，这是散文 |

最后一行是本决策**没有解决**的事，写在 ADR 里而不是埋在注释里。改动 `agent_workspace._runtime_instructions`
与 `codex_runtime._developer_instructions` 的签名在本票被源码级判据钉住
（两者都不接受入口/租户参数），这是「护栏文本不随入口变化」的可执行形式。

## 被否掉的方案

- **诊断路由改用 `assistant_identity`**（拿到 `PlatformDecision`）。这是最"整齐"的
  做法，`platform_identity` 的 docstring 也正是为了消灭重复才存在。它输在**爆炸半径**：
  未带 `X-Business-Entry` 的双平台会话今天在诊断面成功（范围按最窄的 self 取），
  改用平台决议后会变成 `PLATFORM_AMBIGUOUS` 403。而走本决策的形状，同一条请求的
  行为逐字不变（入口为空 ⇒ `is_operator_entry` 返回 False ⇒ 与 consumer 同形）。
  为「整齐」换一次对外行为变更，收益不成立。
- **给 `select_customer_agent` 加 `agent_type` / `require_knowledge` 参数**，不新增函数。
  重复确实少了一处，但两者的谓词方向相反：问答路径「没有 KB 就不服务」，诊断路径
  「没有 KB 照常服务」。塞进一个函数后，每条分支都要读两遍才敢改，而两侧的测试
  也不再能独立钉住各自的语义。
- **把 `business_entry` 放进 `ScopeContext`**。省下逐点透传，代价是它进入
  `scope_fingerprint`——于是同一个会话在两个入口下变成两个作业归属，既有作业与
  会话历史会被这条改动重新切分。这是本决策最贵的一个替代方案。
- **worker 内判定（不触模型、写终态失败）**。本决策采用了它，但值得写下它换来了什么：
  workspace 与作业行在选版之前就已建立，因此 operator 未登记时仍有一行说得出**为什么**
  不被服务，而不是一个永不终止的 202。判据因此是「终态失败 + 不触模型」，不是「创建时 4xx」。

## 后果

- **收益**：运营在 Dify 改运维口径并发布后，下一次管家端诊断用的就是那一版——不需要
  代码发布。`operations` 槽位从死配置变成一条执行的路径。
- **代价**：诊断面多了一次读登记表 + 读 agent store 的往返（每作业一次）。两者都在
  本地 SQLite 上，且只在 operator 入口发生。
- **代价**：`i18n._DIAGNOSIS_ERROR_KEYS` 多一个码。它复用既有的 malformed 文案而不是
  新增十一语种表：调用方无法据文案采取不同行动，能据以行动的是**状态码**。
- **已知上限**：`select_operations_agent` 的「提示词为空即不选」防的是**快照漂移**
  （发布门本身已要求非空），不是防一次合法的发布。
- **与既有 ADR 的关系**：不改 `0001`（只读边界）、不改 `0005`（Codex 中介检索）；
  `0007` 的语言守卫调用点数量不变（`#697` 才新增第 5 点）。本决策属于 PRD #693 的
  ② 号路径，与 ③ 号路径（运维分析面）各落一条记录。

## 验证

- `tests/test_ops_agents.py`：选版语义八条（草稿/名字不符/无名字/类型不符/空提示词/
  停用/跨租户/正常命中）。
- `tests/test_operator_diagnosis_entry.py`：从运行时这一层取证——登记表点名哪一版就
  注入哪一版（换标记可证伪）、非最新版本也被点名命中、consumer 与未采用环境注入为空、
  四种服务不了的情况同为 `failed` + `DIAGNOSIS_AGENT_UNREGISTERED` 且**未触模型**。
- `tests/test_agent_scenarios.py`：空方法论时提示词与本票之前**逐字相同**（字符串相等，
  不是「含有某段」）；有方法论时它排在每条护栏规则之前；护栏文本的生产者签名里没有
  能改变它的输入。
- `tests/test_operator_entry_actions.py`：入口头确实传到了运行时（注入式替身对
  「header 有没有被传到门」是盲的，所以断言的是收到的参数）。
- **未验证（如实注明）**：真实模型下的措辞差异、41 上的生产选版、以及
  「Dify 提示词与护栏冲突时模型听谁的」——后者按设计无强制机制，只能观测。
