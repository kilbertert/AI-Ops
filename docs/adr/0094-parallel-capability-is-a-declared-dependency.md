# 94. 能不能并行，必须是环境的属性，不是「有人记得装」

日期：2026-10-23
状态：Accepted
影响版本：`pyproject.toml`（`pytest-xdist` 进 dev 依赖；`addopts` 带 `-n auto`）、`uv.lock`、
`.github/workflows/ci.yml`、`.sandcastle/sandbox-prepare.sh`、`tools/test.sh`、`README.md`

## 背景

上一轮把「每圈检查跑并行」下发到了三个仓，实测 249.7s → 46.4s。**然后在真实使用里失效了。**

一个 agent 在独立 worktree 里跑全量用了 **279s**。查下去：

- `.sandcastle/implement.md` 确实写着 `uv run pytest -n auto` ✅
- 但那个 worktree 的 venv 里 `import xdist` → **ModuleNotFoundError** ❌
- 于是 `-n auto` 报 `unrecognized arguments: -n`，唯一出路是把 `-n` 去掉 → 静默退回串行

**上一轮我的全部验证都在主检出上做——那里我手工 `uv pip install` 过。**
新 worktree 的 venv 是干净的，`uv sync` 装不出 lockfile 里没有的东西。

## 决策

**`pytest-xdist` 进 `[dependency-groups].dev`，`-n auto` 进 `addopts`。**

判据只有一条：**「这份代码能不能并行跑测试」应当由 lockfile 回答**，而不是由「谁记得
在哪个 venv 里装过」回答。它不在 lockfile 里，就是在说它不需要。

上一轮我明确写过「xdist **故意不进** pyproject」，理由是改 `uv.lock` 会触发 CD 的依赖
漂移门。那个理由**仍然成立**，但它算错了账：不进 lockfile 的代价是**每个新建 venv 都
静默退回串行**，而那是每天都在发生的；进 lockfile 的代价是**一次性人工更新 41 环境**。

## 被否掉的替代形态

- **在每个 checkout 的第一步装它（上一轮的 `tools/test.sh` 与沙箱 hook）。**
  这是上一轮的方案，也的确是它让主检出快了起来。它输在**覆盖面**：只要有人不走那条路
  （直接 `uv run pytest`、新 worktree、agent 自己敲命令），就退回串行，**且不报错**。
  一条需要在每个入口重复一遍的规则，就是一条会被漏掉的规则。

- **改 `deploy-41.sh` 的漂移门，让它忽略 dev 依赖。** 这是我看完「41 不跑测试、
  venv 也确实不需要 xdist」之后想到的，能让这次改动零人工代价。**否掉**：
  那条门的判据是「两份清单逐字节相同」，简单到不会错；改它等于把它变成
  「我判断哪些依赖影响生产」——那个判断错一次，生产重启就 `ModuleNotFoundError`。
  为了省一次人工流程去改一条保护生产的门禁，方向反了。

- **`PYTEST_ADDOPTS` 环境变量 / conftest 钩子。** 上一轮已实测否掉：前者同样无条件生效，
  后者在 ini 解析之后才加载，来不及。

## 后果

- **新 venv 现在自带调度器**：`uv sync --locked --dev` 之后 `uv run pytest` 就是并行的
  （实测 51s vs 串行 245s）。
- **代价（必须说清楚）**：`uv.lock` 变了 → **41 的依赖漂移门会拒绝部署**，直到按
  `docs/agents/env-41-dependency-update.md` 人工更新一次环境。这是一次性的。
- 去掉三处「手工装 xdist」：CI 的一步、沙箱 hook 的一段、`tools/test.sh` 的补装逻辑。
  留下的 `tools/test.sh` 只做「确认 venv 与 lockfile 一致」与转发参数。
- **仍未解决**：worktree 的 venv 需要有人跑一次 `uv sync` 才有依赖——这是所有 Python 项目的
  常态，不是本决策的缺陷。区别在于：**跑过之后它就是并行的，不再取决于装没装过某个包。**
