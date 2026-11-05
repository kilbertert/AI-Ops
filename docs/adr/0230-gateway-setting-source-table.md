# 230. Gateway 设置来源表：一张表、一条回退规则、文件缺失即启动失败

日期：2026-11-05。

Status: Accepted（#973）。

`GatewayServerSettings.from_env` 过去是 76 行手写的「`_env(...) or _file_value(...)`」
链：每个字段各自决定读不读私有 `production.env`，读者要逐行核对才知道某个键放进
`production.env` 到底有没有效 —— 而且确实漏过（`ragflow_console_*` 声明了字段、
路由读了、`from_env` 从没赋值）。`aiops-admin --config` 另抄一份 10 键的合并词表，
是「同一条规则两份实现」。

现在改为一张声明表 `SETTING_SOURCES`：每个字段恰好四个判据（env 键名、是否读
私有配置、类型、默认值），`from_env` 由表驱动，`--config` 合并词表由表派生，
`.env.example` 的键清单由表机械核对。三个路径派生项（`data_home`/`database_file`/
`server_config_file`）留在 `from_env` 手写，因为它们的值由其它键算出，不是
「env 或文件」的形状。

**统一的「缺失才回退」语义**：`os.getenv` 给出 `None`（键不存在）才看私有配置；
键存在 —— 哪怕是 `KEY=` 的空串 —— 就是值，不再向下回退。这是与
`Settings.from_config` 同一条规则，但**相对旧 `or` 链是对外行为变化**：进程 env
里的 `KEY=` 过去会被当成「没配」而落回文件值，现在按显式空值处理（str 字段得
`""`，数值/布尔字段空串没有可辩护的含义、等于默认）。部署侧受影响面实测很小：
本机真实 `production.env` 无任何 `AIOPS_GATEWAY_*` 键；唯一依赖旧语义的是
`deploy/m2-live-03/gateway.env.template` 的空占位行，已改注释并要求显式非空
（`checks.sh` 的非空断言本来就拦住这个形态）。

**缺失即启动失败（Option A）**：`AIOPS_GATEWAY_SERVER_CONFIG_FILE` 显式点名一份
不存在的文件 → `PrivatePathError`；文件存在但权限过宽（非 0600）→ 同一个错，
点名文件与 mode。过去不可读被静默当空表 —— 服务照起、每个请求收误导性的
401/503。默认路径下文件不存在仍是合法形态（记空表），「起监听必须有文件」
那一半由 `validate()` 承担，不变。

`file=False` 的 19 个字段（监听参数、introspection、日志级别、路由阈值等）只读
进程 env：写进 `production.env` 不生效不是漏读，是这一列的显式决定。`.env.example`
给这些键行尾标 `# 仅进程环境`，机械测试双向核对标记与表一致。
`.env.example` 里 introspection 段旧注释「client secret 只放服务器私有
production.env」是写错位置的指引（该键从未读过文件），已按表更正。

机械守卫（`tests/gateway/test_setting_sources.py`）按整表参数化：表↔dataclass 字段
双向完备、kind/默认值对字段核验、env 名唯一、`file=False` 名单是独立台账（不从
表推导 —— 参数化用例读表自己，翻一列它跟着翻）、`from_env` 体内不许再出现
第四个 `AIOPS_GATEWAY_` 字面量（AST 守卫）。`.env.example` 镜像测试钉键集与标记。

## 备选方案

- **每个字段继续手写两行 `or` 链**：现状即问题 —— 76 处手工一致性靠评审维持，
  已经漂过至少一次成静默不生效，否决。
- **整表读 `production.env` 不设 `file` 列**：监听参数、日志级别这类只该由进程
  给的值会变成文件可配，等于把 EnvironmentFile 的职责拆成两处，运维要同时看两
  份文件才知道生效值，否决。
- **不可读回落空表（现状）**：Option A 的另一候选；「什么都没配」把服务起起来
  之后每个请求收误导性错误，把配置错伪装成运行时故障，否决。
- **`KEY=` 空串按「未配置」回退（旧 `or` 语义）**：与 `Settings.from_config` 的
  「显式空串即覆盖」分叉，两套语义并存正是本仓反复踩的坑；统一为「键存在即值」，
  代价是少数依赖回退形态的模板要改成显式非空 —— 已逐一改完，否决。
