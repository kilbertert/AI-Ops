# 76. workspace 的 `ready` 分三步，模板失败不回滚

日期：2026-10-21
状态：已采纳（#881 实施；ADR-0059 §5.1 的第 3 步落成代码）
影响版本：`src/aiops_diagnostics/dify_console_admin.py`（新）、
`src/aiops_diagnostics/dify_workspace_ready.py`（新）、
`dify_workspaces.py`（`mark_ready` / `mark_template_failed`）、
`admin_cli.py`（`ensure-ready`）、`gateway_config.py`（两键）、
`src/aiops_diagnostics/dify_workspace_template.dsl.yml`（新）

## 背景

ADR-0068 把 workspace 建出来就停了，状态是 `created`，并明确写了「建出来的仍是空壳，
#881 才让它能用」。空壳的具体含义是实测的，不是估计：

- 新 workspace 只有 6 行插件**升级策略**，`/plugin/list` 回空 —— 没有模型插件。
- 它没有任何 provider 凭据。Dify 的数据模型里**没有共享层**（`providers.tenant_id`
  非空，凭据用**每租户一把 RSA 密钥**加解密），把行复制到另一个 workspace 也解不开。
- 它没有 app。模板在仓库里，导入时还没有这个 workspace 自己的 provider 与知识库 UUID。

三步因此缺一不可，且**顺序**不是风格问题。

## 决策

**一、`created` → `ready` 是一次单行推进，只由三步全部成功触发。**

`created` 的含义是"workspace 存在"，`ready` 是"它能真的跑一次对话"。用户撞上 `created`
时该看到"正在配置"，撞上 `ready` 才该看到正常工作。解析函数对两者都返回 id（ADR-0067），
所以这个区别只影响**人看到的文案**，不影响是否 fail-closed。

**二、顺序是插件 → 凭据 → 模板，不能换。**

模板 DSL 会引用 provider。provider 没有凭据时**导入照样成功**，但那个 app 一跑就报
`ProviderTokenDoesNotExist` —— 把失败从"开户这一步"推到了"运营点了一下才发现"。
所以前两步失败就停，不发第三步。

**三、模板失败**只记状态**，不回滚。**

| 失败点 | 写入 | 为什么 |
|---|---|---|
| 插件 | `created` 不动（抛错即止） | 重跑一次是安全的；插件安装是幂等的 |
| 凭据 | `created` 不动 | 同上；失败一定是"这把 key 不能用"（`DIFY_CREDENTIAL_INVALID`） |
| 模板 | `mark_template_failed`：状态**仍是 `created`**（已是 `ready` 的保持不变），`last_error` 记原因 | workspace 与凭据都在，人进得去、能改；重跑补齐这一步 |

第三行是这一票里唯一不能靠"重试"擦掉的判断。写成 `failed` 会让 `resolve()` 把它当成
"没建出来"，而下一次 `ensure-workspace` 的 `claim` 会**重建** —— Dify 没有删 workspace
的 API，那个重建出来的就是永久孤儿。**"能用的东西只差一步"与"没建出来"必须分开。**

**四、模型凭据来自部署配置，代码里没有密钥。**

`AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY` 由运维在主机上填；缺了就拒绝并点名这个变量，
不回落任何"示例 key"。这不是洁癖：Dify 写凭据时会拿它**做真实校验**（2026-10-21 实测，
假 key 回 400 `Credentials validation failed ... 401`），所以假 key 走不通，而真实 key 不
该出现在仓库里。

**五、三条动作全部幂等，判据都是"先看有没有"。**

安装是**异步**的：`/plugin/install/pkg` 立刻回一个 `task_id`，插件真正出现在
`/plugin/list` 里是几秒之后。所以发完请求要轮询 `/plugin/tasks/<id>` 到 `success` 再往下
走 —— 不等的话，紧接着的写凭据会因为插件还不在位而报"provider 不存在"，而那个报错会被
读成"配置错了"。任务 `failed` 与超时**都算失败**，且各自一个码
（`DIFY_PLUGIN_INSTALL_FAILED` / `DIFY_PLUGIN_INSTALL_TIMEOUT`）：前者要人看原因，后者
重跑一次通常就好（已装的会被跳过）。

| 动作 | 幂等判据 |
|---|---|
| 装插件 | `/plugin/list` 里已有的不再发安装请求；**发出后等任务落地**（见下） |
| 写凭据 | 写后**回读并切到生效位**（ADR-0078）；同名凭据已存在就跳过（`aiops-workspace` 是固定名）。判据读 `GET …/model-providers` 的 `custom_configuration.available_credentials` —— **不是** `<provider>/credentials`，后者回的是当前生效那一份的字段字典、没有名字列表；同名再写 Dify 回 400 `already exists`，那是**已达成**而非失败 |
| 导 DSL | app 名字已在 `/console/api/apps` 里就跳过 |

## 被否掉的替代形态

- **先导模板、后写凭据。** 导入成功、运行失败，失败点被推到用户第一次提问时。
- **模板失败就 `mark_failed` 重来。** 重来 = 重建 workspace = 永久孤儿（见上）。
- **把 provider 凭据复制到新 workspace。** Dify 用每租户 RSA 解密，复制过去的行解不开。
- **走 `/plugin/install/marketplace`。** 36 上 `MARKETPLACE_ENABLED=false`，回 400
  `marketplace is not enabled`。用的是 `/plugin/install/pkg`（body 带
  `plugin_unique_identifiers`）。

  **但 `install/pkg` 有一个主机级前置**：daemon 的**包桶**里必须已经有那个 `.difypkg`
  （`GetPackage` → `plugin package not found, please upload it firstly`）。放包进去的是
  `POST /plugin/upload/pkg`，取包的是 `fetch-manifest`（走 `ssrf_proxy` 白名单）。
  所以"这台 Dify 能不能装插件"是一个**运维决定**，不是每个租户开户时能自动完成的步骤。
  本命令把它说清楚（`DIFY_PLUGIN_PACKAGE_MISSING` 单独一个码、文案点名 upload），
  但**不替运维决定出口**。2026-10-21 核对：36 的包桶里已有 deepseek/openai 两个包。
- **用 `/console/api/workspaces/switch` 切工作区。** 它把该账号其它 join 的 `current`
  全置假并提交，对并发请求可见；后台动作走 `ADMIN_API_KEY + X-WORKSPACE-ID`。
- **模板里钉死模型名。** 模型由该 workspace 自己的 provider 凭据决定；钉死会让"换模型"
  变成"改模板 + 重导一遍"。

## 后果与代价

- **模板随包发布，不在 `ops/`**：41 的部署产物只含 `src/`，放 `ops/` 会让"41 上跑
  `ensure-ready`"报文件不存在 —— 而 41 正是这个命令要跑的地方。读法与 `faq_catalog.json`
  相同（`importlib.resources`）。
- **模板的 `dataset_configs` 不带 `datasets` 键**（不是 `datasets: []`）：Dify 1.17.1 的读取端期望
  路由对象 `{strategy, datasets: []}`，给列表会在 `.get()` 上抛 `AttributeError` —— 那个 app
  第一次跑就 500。省略是真实导出里"没挂知识库"的形状。
- **模板 app 不能被 `pull-dify` / `sync-from-dify` 消费**：`dify_dsl_pull._required_model`
  对空的 `model_config.model.name` 显式失败关闭，而本模板刻意留空。它是"结构占位"，
  不是运营要编辑的那个智能体 —— 运营在新 workspace 里选一次模型之后才谈得上拉取。
- **模板 app 是一个空壳 app**：`model_config.model` 与 `dataset_configs.datasets` 刻意留空
  （模板文件的头注释记了理由）。它与"workspace 可用"不冲突 ——
  插件与凭据到位之后，运营在控制台里选一次模型就能跑。
- **`ready` 之后仍可能缺 app**：模板导入失败只记状态，所以一个 workspace 可以在 `ready`
  之前的任何一轮里停下。**重跑 `ensure-ready` 是补齐手段**，它显示在错误文案里。
- **插件标识钉的是版本 + sha**（`provider/name:version@sha`）。Dify 升级插件后这两个标识
  可能不再是最新，但"能跑"不依赖它们是最新的 —— 换版本是一次显式的改动。
- **没有在真机上跑完三整步**（截至本 ADR）：模型 key 未在 41 上配置。本轮证据是单元测试
  （假 Dify 控制台）+ 只读的真实接口探测。
