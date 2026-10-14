# 工具白名单：Dify 声明，执行器强制，清单不许承载

日期：2026-10-14
状态：已采纳（2026-10-14 **修订**：工具识别规则改为「`aiops` 提供方 + `operationId` 对齐」，见 §修订）
影响版本：PRD #577（Dify 作为权威编辑面）；PRD #772 E（3A 工具目录）；`agent_contracts.ALL_TOOL_NAMES`、`AgentConfig.allowed_tools`、`agent_manifest.DIFY_OWNED_FIELDS`

## 背景

PRD #577 让 Dify 成为智能体的编辑面。到这一步为止，Dify 能管的字段是**正文**（提示词）
与**知识库**（dataset 经登记表翻译成我们的 KB id）。**工具**还完全没有：七个
`ToolName` 硬编码在 `diagnostic_tools.TOOL_DESCRIPTIONS` 里，提示词把它们**全部**广告
出去（`agent_engine.py` 的 `_initial_prompt`），执行器**没有任何白名单**。

也就是说：**运营无法收窄任何一个智能体能用哪些工具**，而"这个 app 能碰什么数据"
恰恰是 PRD 说的「Dify 只编排、不授权」里最需要运营能表达的一维。

## 决定

**三层同时改，少一层就是「看着像管住了」。**

1. **`AgentConfig.allowed_tools: tuple[str, ...]`，默认 = 显式写出的全部七个 `ToolName`**
   （`agent_contracts.ALL_TOOL_NAMES`）。默认写成"全部"是刻意的：它让这个字段
   **对存量零影响** —— 没有快照会因为多了这一维而比较失衡、被全量重发。
   fail-closed 体现在**执行器**，不在字段默认值。
2. **强制在执行器**（`diagnostic_tools.DiagnosticToolExecutor.execute`）：白名单外的工具
   记一条 `blocked` 证据、**不取数**。**先于 journal 缓存判定** —— 否则一次早先运行的
   成功结果会被当成"允许"返回，白名单看起来生效了而数据照样流出。这是 ADR-0015 §五
   「提示词不是强制机制」的直接应用。
3. **发布门**（`AgentManager._validate_config`）：白名单里的名字必须是已枚举的 `ToolName`。
   空名单**合法**（"一个工具都不许跑"是真实意图），这条门只查"名字认不认得"。

**工具白名单由 Dify 拥有，清单不得承载（R3）。** `agent_manifest.DIFY_OWNED_FIELDS`
把 `prompt` 与 `allowed_tools` 收成**一处**具名集合：

- 装载期：绑定了 Dify 的 agent 若在 `[[agents]]` 里写了 `allowed_tools` ⇒ **拒**（两个权威）。
- 未绑定的 agent 没写 ⇒ 哨兵**归一成 `ALL_TOOL_NAMES`**（否则那个不可能匹配的字符串
  会被发布门当成"未知工具名"拒掉，一个合法清单会装载失败）。
- `_converge_one`：Dify 拥有的每个字段都**保留库中现值**。漏掉这一步的真实后果是：
  运营在 Dify 里把工具收窄成一个，下一次 `reconcile` 用默认值（全部）覆盖掉它，
  而报告只说 `unchanged`。

**拉回侧 fail-closed**：DSL 的 `agent_mode.tools` 若**声明了**工具而调用方没有给出
翻译器 ⇒ **拒**，点明那是 Dify 的工具身份；什么都没声明 ⇒ 不加约束（存量行为）。

## 修订（2026-10-14，PRD #772 E / 3A）：从「显式绑定」改为「`aiops` 提供方 + `operationId` 对齐」

上面那条「映射键定为 `provider_id/tool_name`」是**当时唯一可行**的做法：仓内没有
真实的**外接 API 工具**样本，只有一个内置工具（`time/timezone_conversion`），而内置
提供方的 `provider_id` 恰好是个短名。现在 3A 要在 Dify 里注册一个**自定义 API 提供方**
`aiops`，把七个真实工具暴露成可勾选的条目 —— 那条命名空间就撑不住了，原因是**事实**，
不是偏好：

- **自定义 API 提供方的 `provider_id` 是它数据库行的 UUID**（`ApiToolProvider.id`）。
  Dify 1.17.1 源码 `ToolManager.get_api_provider_controller` 就是按
  `ApiToolProvider.id == provider_id` 查的。所以「按名字匹配」在 API 提供方上根本不存在：
  条目里写的是一个 UUID，不是 `aiops`。
- **条目里没有提供方名字这个字段**。`core/agent/entities.py` 的 `AgentToolEntity` 是
  `(provider_type, provider_id, tool_name, tool_parameters, plugin_unique_identifier,
  credential_id)`；DSL 导出（`app_dsl_service.py`）也只把 `credential_id` 抹掉，
  不补名字。⇒ PRD 初稿写的 `provider_name == "aiops"` **在导出形状里没有可读的字段**。
- **那条短名映射只是巧合，不是契约**。`provider_id/tool_name` 作为字符串键能成立，
  靠的是"UUID 里不含 `/`"。把它当契约，等于把两个提供方的**同名工具**放进同一个
  命名空间 —— 而「名字相等不代表语义相同」正是本 ADR 上一节立下的纪律。

**决定（两层）：**

1. **`aiops` 提供方下的条目按 `provider_type == "api"` 且 `provider_id == <该提供方
   的 UUID>` 匹配**，键是 `operationId`，**直接映射到同名 `ToolName`**。七个之外的名字
   **拒**（报出条目），不按字面搬运。
2. **其他提供方继续只走 `AIOPS_GATEWAY_DIFY_TOOL_BINDINGS`** 显式登记表。这条**不变**。

**那个 UUID 不落任何地方是这一节的重点。** PRD 要求「重建 Dify 实例或重建提供方后已
发布的映射不失效」；把 UUID 写进配置/清单/代码都会违反它。所以 UUID 是**拉取时当场**
从控制台 `GET /console/api/workspaces/current/tool-providers?type=api` 解析出来的
（`DifyDslClient.tool_provider_ids`），只活在一次拉取的调用栈里。重建之后重新解析出来的
新 UUID 一样能用 —— `test_the_same_dsl_under_a_different_provider_uuid_resolves_the_same_way`
是这条的可执行形式。

**第二个不变式（保持 ADR-0019 原意）：执行器仍是唯一强制点。** 这里解析出的白名单只是
一个 `AgentConfig.allowed_tools`，与拉取路径的其他字段同源同门；白名单外的工具在执行器里
记 `blocked`、不取数。目录端点（`POST /v1/dify/tools/<tool>`）**不执行任何工具**，
只回一段固定说明 —— 它在 Dify 侧的存在意义是"让运营**看得见、勾得到**"，而 Dify 自己的
智能体循环碰不到我们的执行器，所以预览结果**本来就不代表生产行为**（@772 F 的试运行
是唯一的真实路径）。

**`enabled=false` 与「删除」同义**（PRD #772 E）：不进入白名单。而**全被关掉 / 什么都没勾**
⇒ 沿用 ADR-0019 的默认（全部七个）—— 与"不声明工具"逐字同向，理由是同一个：把一个工具
切到关闭、而它本来就是默认全开的，不该产生一次"白名单变空"的静默收窄。

## 被否掉的替代形态

- **只在提示词里收窄。** 那只是告诉模型"可以要什么"，模型请求白名单外的工具时执行器
  照跑 —— 与 ADR-0015 §五 已经立下的纪律相反，而且这种"管住了"是**测不出来**的：
  提示词里没有那个名字，看起来就是管住了。
- **把默认值写成空表**（"默认什么都不能跑"）。它会把每一个存量 agent 在下次发布时
  变成"不能调工具"，而运营没有做过这个决定 —— 一次字段新增不该改变任何线上行为。
- **`allowed_tools` 放进清单**。那会让同一个字段有两个权威，且失效方向很坏：
  reconcile 静默覆盖运营的改动，报告说 `unchanged`。
- **发明一个 Dify 工具名 → `ToolName` 的映射键。** 本 ADR 初版写下这句时，仓内
  **没有任何真实样本**（36 上两个 app 的 `agent_mode.tools` 都是空的），所以当时
  **不发明它**，只把声明报出来要求调用方拒。

  **2026-10-14 补齐**：在 41 的生产 app 上**真挂了一个工具**（`time` 内置工具的
  `timezone_conversion`），读回导出形状 —— 于是样本有了：

  ```yaml
  agent_mode:
    enabled: true
    strategy: router
    tools:
    - provider_id: time
      provider_type: builtin
      tool_name: timezone_conversion
      tool_parameters: {}
  ```

  **⇒ 映射键定为 `provider_id/tool_name`**（`time/timezone_conversion`），登记在
  `AIOPS_GATEWAY_DIFY_TOOL_BINDINGS`（`dify_id:our_tool`，与知识库那张表**同形**）。
  未登记 ⇒ **拒**，点明那个 Dify 身份，绝不按字面搬运 —— 因为两个命名空间里
  **名字相等不代表语义相同**（它那边的 `order_snapshot` 与我们的不是同一个东西）。
- **删掉 `ZERO_ORDER_ALLOWED_TOOLS` 就地替换成它。** 一并删（它是死代码：只有自身与
  它的测试引用，生产路径从不调用），新白名单在**执行器**里的那个才是真的强制。
  留着一个没人调的守卫比没有更糟 —— 它让人以为有保护。

## 后果

- 存量快照、存量清单、存量发布**逐字不变**：默认值即旧行为。
- 运营在 Dify 里挂工具**已经能生效**：`aiops` 提供方下的七个直接生效（见 §修订）；
  其他身份登记进 `AIOPS_GATEWAY_DIFY_TOOL_BINDINGS`（`provider_id/tool_name:our_tool`）。
  翻不出的身份**会被拒**，点明是哪一个 —— 拒比静默忽略诚实。
- **3A 上线后 `time/timezone_conversion` 这条占位映射退役**：它证明的是翻译管道通了，
  不是"运营能挑真实工具"。登记表的**解析能力保留**（第三方提供方仍走它）。
- `agent_mode.enabled=false` 时 tools 是惰性的，但仍会被读 —— 运营挂了工具却没开开关，
  应该听到**工具**这件事，而不是一个他没设过的开关。
- 本 ADR 只覆盖**诊断路径**（`operations` agent）。客服路径没有工具执行器，因此不获得
  工具能力，只获得 starters 覆盖（另见 #585 的反转）。
