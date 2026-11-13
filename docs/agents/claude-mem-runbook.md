# Claude-Mem 本机接入与验收

## 已确认范围

保留 Claude Code 与 Codex 各自入口，以 Claude-Mem 共享工作上下文。摘要模型沿用用户指定的
`claude-deepseek` 通道；本轮固定 Claude-Mem `15.9.0`。正式长期记忆仍遵循原有人工确认流程。

先用独立数据目录和合成任务验证；不导入历史会话，不开启 CMEM 云同步。正式 worker 由
`systemctl --user` 管理，只监听已登记的 loopback 端口。插件配置保留既有 hooks 与 MCP。

## 验收合同

```gherkin
Feature: Claude Code 与 Codex 共享工作上下文
  Scenario: Codex 的工作可以由 Claude 接手
    Given Codex 来源记录了合成任务目标、已完成步骤和未完成步骤
    When Claude 来源在相同项目请求启动上下文
    Then 返回已完成与未完成信息以及来源指针
  Scenario: Claude 的工作可以由 Codex 接手
    Given Claude 来源记录了另一条合成任务的决策和下一步
    When Codex 来源在相同项目请求启动上下文
    Then 返回该决策和下一步而不要求读取对方私有日志
  Scenario: 项目之间保持隔离
    Given 两个项目各有不同的合成任务
    When 请求其中一个项目的上下文
    Then 不注入另一个项目的记录
  Scenario: 摘要通过指定通道完成
    Given 摘要使用 claude-deepseek 的连接配置
    When 写入一条合成工具观察
    Then 能检索到模型生成的观察记录
    And 摘要进程的工具权限保持禁用
```

## QA 步骤

CM-01：创建临时数据根与自动分配的 loopback 端口；运行固定版本 worker；通过健康检查。
CM-02：经 Claude/Codex hook 输入合成数据，等待观察保存；双向请求 context，核对特定事实。
CM-03：用不同项目重复 context 请求，确认不注入不相关记录。
CM-04：检查真实摘要模型返回、worker 持久化结果及进程权限；不以 HTTP 200 代替摘要成功。
CM-05：安装前后比较既有配置条目；服务只绑定登记端口，重启后健康且可恢复上下文。
清理：停止本轮试验进程，保留最小结果；真实数据目录与试验数据目录分开。

## 当前安装与使用

Claude-Mem `15.9.0` 已通过官方安装器安装，Claude 与 Codex 保留各自入口。
用户只授权 AI-Ops：通过各 checkout 的本地插件开关启用，其他项目默认不启用。
Claude 的用户级开关不写该插件（不是 false）；Codex 用户级为 false，项目级覆盖为 true。
原因：Claude-Mem worker 读取用户级 false 后会直接退出，不认识项目覆盖；不写用户级开关，
Claude 宿主仍默认不加载，而 worker 可以由 systemd 管理。

- 查看器与健康检查：`http://127.0.0.1:12112`、`/api/health`、`/api/readiness`。
- 服务：`systemctl --user status claude-mem.service`；必要时 `systemctl --user restart claude-mem.service`。
- 私有数据：`~/.claude-mem/`；配置 `settings.json`，凭据 `.env`，SQLite `claude-mem.db`。
- 模型：`CLAUDE_MEM_MODEL=deepseek-latest`；隔离观察的实际返回模型为 `deepseek-v4.1-flash`。
  这是可变别名，本次结果不保证将来仍为同一模型。
- `CLAUDE_MEM_SESSION_START_INCLUDE_ALL_SOURCES=true`；项目身份使用 `git-remote`，
  同仓库的 worktree 可共享记忆。任务接力仍需明确任务、分支和现场，不能把最近记录当当前任务。
- 云同步、遥测、历史批量导入、Chroma、文件生成、额外待办账本均关闭；检索先用 SQLite。
  当前范围不含语义向量检索。自动摘要仍向用户选定的 DeepSeek 通道发送采集内容。

Codex 新装或更新后需在 AI-Ops 打开 `/hooks`，核对并信任 `claude-mem@claude-mem-local` 的 7 个 hooks。
本次已由用户完成，原生接口复核全部 `trusted`。未信任时插件可被列出、MCP 可加载，但自动采集
不会执行。不要用 bypass 选项代替此确认。
Claude/Codex 都应重新开始会话以加载插件。可要求 agent：
“从 Claude-Mem 查这个任务的决定、验证结果和下一步，先核对当前分支，再继续。”

## 新建 worktree

本地开关不提交，也不会自动出现在新 worktree。在 `dev-worktree start` 创建的新 AI-Ops
checkout 中，合并以下配置，保留原有字段：

`.claude/settings.local.json`：

```json
{"enabledPlugins":{"claude-mem@thedotmack":true}}
```

`.codex/config.toml`：

```toml
[plugins."claude-mem@claude-mem-local"]
enabled = true
```

这两个路径已在本仓库本地 Git exclude 中忽略。若某个路径后来改为受版本控制，必须转为
任务分支交付，不直接改默认分支。验证有效配置：

```bash
codex plugin list --marketplace claude-mem-local --json
claude plugin list --json
```

## 更新与回滚

更新前备份私有 `settings.json`、`.env`、两端插件注册与配置；不要将它们上传仓库。
官方安装器的可复用入口（版本先经验证再替换）：

```bash
CI=1 DO_NOT_TRACK=1 CLAUDE_MEM_TELEMETRY_ERRORS=0 \
  npx --yes claude-mem@15.9.0 install --ide codex-cli --provider claude \
  --runtime worker --no-auto-start
```

`CI=1` 在这里仅让安装器不生成可选 CMEM 登录请求。该命令会注册两端插件并可能重新全局启用，
所以先关闭采集，再恢复本机仅 AI-Ops 的启用范围；不能把安装完成当作范围复核通过。
不要传 `--model deepseek-latest`：15.9.0 安装器的参数校验拒绝它，运行配置却支持该别名。
安装器选择 Claude provider 还会清空网关 `.env`；必须在本机从 `claude-deepseek` 所用受控
settings 文件的 `env` 中重新读取 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN` 写回 0600
文件，不能打印值或从会话复制。凭据源有变更时也用同一方式刷新。

摘要进程直接使用 Claude 可执行文件和专用 `.env`，不使用追加 `bypassPermissions` 的
交互启动器；因此复用 DeepSeek 通道而保留上游 `tools=[]`、`dontAsk` 与 MCP 隔离。
重启服务后先核对 `/api/health` 的 authMethod 为 Gateway，再做合成采集确认模型。

回滚：停用 AI-Ops 各 checkout 的两个本地插件开关，再执行
`systemctl --user disable --now claude-mem.service`；数据目录保留供人工检查。
若要删除端口登记，仍走治理 PR 与完整 `install.sh`，不能手改已安装的端口表。

## 2026-11-05 验证结论

- CM-01：临时端口 worker 健康，正式 12112 worker 健康并在用户服务下运行。
- CM-02：两种 hook 输入分别生成观察/摘要；对方 context 取回目标、约束和下一步。
- CM-03：两个独立 Git 项目的 context 不混入另一项目合成标识。普通子目录不构成项目边界。
- CM-04：摘要实际模型 `deepseek-v4.1-flash`；原生 Claude Read 产生观察，经 MCP 查询取回。
- 原生 Codex 再验：通过插件 MCP `get_observations` 读到原生 Claude 写入的 CEDAR-985 / 43，
  未读取 Claude 日志、未执行 shell；跨工具检索可用。
- CM-05：既有 hooks 未修改；用户信任后，Codex 原生接口返回 7 个 `trusted` hooks。
- 原生 Codex 自动摘要→Claude 启动注入：MAPLE-986 的重试上限 11、测试未运行、下一步
  验空输入拒绝被新 Claude 正确复述；接收方没有调用工具，也没有读取 Codex 日志。

Claude 的无工具 print 会话可完成回答，但本轮未观察到它自动生成摘要；不能以该结果宣称
所有退出路径都已覆盖。双端共享的原生合成验收已通过，不证明任意长任务的完整保真。
只读 Codex shell 试验曾被 `bwrap` 的 loopback 权限错误阻断，未放松沙箱；改用只读 MCP
完成任务，且失败事件本身已自动记录。该环境故障不在本轮修复范围内。
结果见[验证记录](../validation/2026-11-05-claude-mem-pilot.md)。未完成业务验收。

## 2026-11-05 运行时故障与修复

**平台事实：Codex 写完 hook payload 后可能保持 stdin 打开。** 读不到 EOF 的 hook 会卡到
宿主超时并被记为 "Hook failed - hook timed out"。claude-mem 的 bun-runner 已用
「缓冲区能完整解析 JSON 即返回」规避；ponytail hooks 有 1s 兜底定时器。

当日实测的超时来源与处置：

- `pebrel-hook`（`~/.local/share/pebrel/ai/bridge.py`）`sys.stdin.buffer.read()` 读到 EOF
  → 每个事件 3s 超时。**已在本地给 `bounded_payload` 换成有界增量读**（首个完整 JSON /
  EOF / 1.5s 先到为准）；上游包更新可能覆盖，复发时先核对这里。
- `herdr-agent-state.sh`（受管文件，不可改）`cat` 读 stdin → SessionStart 10s 超时。
  hooks.json 命令已改为先经 `~/.codex/hook-stdin-drain.py` 排空再喂给它；命令变更需要
  在 `/hooks` 重新信任该条。
- `team_memory.hook_runner` 同款 read() → SessionStart 15s 超时。修复走
  Agent-Team-Memory PR #31（`_read_stdin_bounded` + 管道级测试）。
- claude-mem 的 30s/5s 超时仅发生在安装窗口（worker 未起/重启/初始化重活）。worker 由
  `systemctl --user` 管理（Restart=on-failure），worker 不可达时 hook 0.5s 内 fail-open
  并进 spool，不会卡 30s；若再现整屏 30s，先 `systemctl --user status claude-mem`。

启用面已补齐：AI-Ops 主 checkout 与全部 worktree 的 `.codex/config.toml`、
`.claude/settings.local.json` 均已开启本插件（gitignored，不入库）。
