# 69. 运维 CLI 按租户解析 workspace，且任一解析失败整条拒绝

日期：2026-10-21
状态：已采纳（#886 实施；ADR-0059 §5.2b 落成代码）
影响版本：`src/aiops_diagnostics/admin_cli.py`（`pull-dify` / `sync-from-dify`）

## 背景

`admin pull-dify` 与 `admin sync-from-dify` 各自构造一个 `DifyDslSource(workspace_id=…)`，
而那个值一直是**一个配置常量** `AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID`。单 workspace
时代它是对的；多 workspace 之后它变成一条真实的越权路径：

- `pull-dify` 把 A 租户的草稿**发布进**那把常量指向的 workspace；
- `sync-from-dify` **遍历注册表里的多个租户**，于是同一个常量被套到 N 个租户上 ——
  它是"一个 workspace_id 套到 N 个租户"最明显的例子。

## 决策

**一、两条命令都按 `binding.tenant_id`（`pull-dify` 是 `--tenant`）解析 workspace，
常量不再参与。**

解析走的是 #879 的那个 `resolve_workspace`，四个失败码原样保留 —— 它们各自对应不同的
下一步（缺行去开户 / `creating` 稍后重试 / `failed` 重建 / `unknown` 先对账），
合成一句话就把那个区别丢了。

**二、`sync-from-dify` 先**全部解析、再开始同步**。**

这是本票的核心判据，不是实现细节：

- **逐个"边解析边同步"** 会让前半段已经写进生产，而退出码仍是非零 —— 那时"部分成功"
  已经发生，回滚无从谈起。
- **"跳过解析不出的租户继续跑"** 更糟：一次 sync 漏掉半个租户却退 0，运营会以为都同步了。
  这是最难发现的一种失败。

所以顺序是：收集全部绑定 → 逐个解析 → **任一个失败就什么都不写并退 1**，输出里点名是哪个
租户、哪个码 → 全部成功才开始同步。

**三、部署身份 `AIOPS_GATEWAY_ENVIRONMENT` 与其它控制台键同源。**

两条命令都经 `_dify_console_and_db` 读 `--config` 指定的文件。部署身份也加进了那个
合并列表，理由与 #625 完全相同：41 上的调用形状是
`--config /etc/aiops-41/production.env`，而 systemd 的进程 env 指向另一个文件。
只从进程 env 读部署身份会让"文件里写好了、命令却说没配"重演一次。

## 被否掉的替代形态

- **保留常量作为默认值**（配了就用租户的，没配就退回常量）。多 workspace 之后没有
  "合理的默认"：退回去就是把 A 的正文发进 B 的 workspace，而**成功**是它最坏的性质。
- **解析失败就跳过那个租户、其余照跑，退出码非零**。退出码对，但写已经发生了，
  而"哪些写了、哪些没写"只存在于一次运行的输出里。
- **解析失败时回滚已同步的版本**。`AgentManager` 没有 published→旧版本的路径
  （`current-delivery-state.md` 已记：版本号前进而内容回到旧值只能再发布一版），
  所以"回滚"本身就是一次新的生产变更。**先全部解析**才是真的回滚。
- **在 `pull-dify` 的 `--tenant` 缺省时按 `--app-id` 反查注册表**。那会让一次装配动作
  变成一次猜测；`--tenant` 是显式参数，本来就在调用形状里。

## 后果与代价

- **多了一次地方读取**（`dify_workspaces` 每次运行读一次全表/N 次）。这是本地的 SQLite，
  代价可以忽略；换来的是"每个租户用它自己的 workspace"这个不变量。
- **`AIOPS_GATEWAY_ENVIRONMENT` 成了这两条命令的新前置**：缺它 ⇒ 退 2 并说清楚原因。
  与 `ensure-workspace` 同一条守卫，缺了是同一种表现。
- **这次没有改网关运行时**（`gateway_api.py` 里那两处 `dify_console_workspace_id` 属
  #884 / #885）。本票只覆盖两条运维命令，因为它们是设计里点名的"最明显"的例子，
  且验收不需要真实 workspace —— 用映射表的一行就能证明。
