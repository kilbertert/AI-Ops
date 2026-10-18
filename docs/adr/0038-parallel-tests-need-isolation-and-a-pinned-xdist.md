# 38. 测试可以并行了：隔离 FAQ 合并测试，xdist 走 --with 不进 lockfile

日期：2026-10-18
状态：Accepted
影响版本：`tests/test_merge_faq_i18n_tool.py`（重写为私有工作树）；`.github/workflows/ci.yml`（Tests 步骤并行）；不改任何运行时代码、不改测试断言

## 背景

ADR 0037 修掉了 `shutdown()` 的 0.5 秒轮询等待（全量 299.62 s → 218.65 s），并把
**并行明确推迟**，理由是 `tests/test_merge_faq_i18n_tool.py` 的 8 个测试直接改写真实仓库资产。

本记录做掉那一步。ADR 0037 定的验收判据是：

> ① 逐文件对比串行/并行结果；② 或者先证明没有共享可写状态。

**两条都做了**，而不是「全量跑三次都绿」——那正是上一轮侥幸通过的判据。

## 诊断

### ① 逐文件对比（复现 0037 的观察）

```
tests/test_merge_faq_i18n_tool.py
  -n 0 :  9 passed
  -n 4 :  4 failed / 5 passed
```

与 0037 记录一致，可复现。

### ② 共享可写状态：**只有这一个文件**

对 `tests/` 全目录做 AST 扫描（找出所有从 `Path(__file__)` / `parents[1]` 传递派生的
路径，再看它们是否被写）：只有 `test_merge_faq_i18n_tool.py` 写真实仓库资产。
`test_agent_workspace.py` 与 `test_diagnostic_tools.py` 有同名形态，但逐个读过——
它们把 `project_root` 只当**只读模板源**，所有写入都落 `tmp_path`，是误报。

另有两条独立扫描确认没有别的并发坑：

- **固定端口：0 个。** 全部 `HTTPServer(("127.0.0.1", 0), …)`，端口由内核分配。
- **绝对路径写入：0 个。** `/tmp/...` 字面量只出现在**校验型**测试里
  （如 `GatewayServerSettings(data_home=Path("/tmp"), …)` 只为触发 `ValueError`），
  从不创建或读取那些路径。

### 决定性对照：并发读者

0037 观察到的坏读取是**间接推断**的。这次直接造出来。写一个与
`FAQCatalog.bundled()` 同形的裸读者（`read_text` + `json.loads`），在测试运行期间
连续读仓库里的 `faq_catalog.json`：

| 测试版本 | 读取次数 | **坏读取** | 读到几种内容 |
|---|---|---|---|
| 旧（就地改写 + finally 还原） | 3832 | **72** | 6 |
| 新（私有工作树） | 3749 | **0** | 1 |

旧版确实在并发读者眼皮底下改写仓库资产；新版一次都没碰。

## 决策

### 1. 测试改成在私有工作树上跑

函数级 fixture 把工具与它的全部输入复制进一个临时树，在树内 `git init` + 基线提交，
然后在树内跑工具。

**为什么需要那次基线提交**：工具的「内容是否变化」守卫比的是
`git show HEAD:<path>`（**已发布**的修订，不是它刚读到的输入——见
`_committed()` 的 docstring）。没有提交，守卫就比空，每个用例都会被判成「已变化」。

**为什么复制 55 ms / 次是可接受的**：一个 session 级模板建一次（55 ms），
每个用例 `copytree` 13 ms；`-n auto` 下每个 worker 各建一次模板。整个文件仍在 1.1 s 内跑完。

**`core.hooksPath` 在副本里清空**：本机的托管钩子拒绝在默认分支提交、且要求
Conventional Commits。这两条对一次性 fixture 没有意义。

### 2. 并行走 `--with`，**不进 `pyproject.toml`**

这是本记录最重要的一条。

把 `pytest-xdist` 加进 `[dependency-groups].dev` 会让 `uv lock` 重写 `uv.lock`，
而 `deploy/deploy-41.sh` 的依赖漂移门拿 `uv.lock` 的 sha256 与 41 上的比
——**「加一个测试依赖」会拦住整条 CD**，直到有人按
`docs/agents/env-41-dependency-update.md` 的人工流程更新 41 的环境。

这不是推测：`tests/test_cd_deploy_scripts.py::_cd_push_paths` 的 docstring 已经写明了
这条约束（它为了避开这个门，宁可手写缩进解析也不引 YAML 库）。实测确认：
加了 xdist 后 `uv.lock` 哈希变化。

所以 CI 用：

```yaml
uv run --with pytest-xdist==3.8.0 --with execnet==2.1.2 pytest -q -n auto
```

`--with` 把 xdist 装进 uv 的**临时覆盖环境**，不改 `uv.lock`、不改 `pyproject.toml`
——实测跑完两者哈希均未变，`uv sync --locked --dev` 仍然通过。

**两个包都写死版本**（`pytest-xdist==3.8.0` 与它的直接依赖 `execnet==2.1.2`）。
这是 review 抓出来的：第一版只钉了 xdist，而 `execnet>=2.1` 会被无版本约束地解析
——「钉子只钉了直接依赖」。`pytest` 本身由上面的 `uv sync --locked --dev` 提供，
在 lockfile 里。

### ⚠️ 确定性边界（不要把它说成"逐字节可复现"）

`--with` 的解析**不在 `uv.lock` 里**。所以这个门禁的**依赖图**不是被锁死的，
只是**直接依赖被写死了版本**；`execnet` 的元数据若日后变化，解析结果仍可能与今天不同。

那它凭什么满足 `deterministic-ci`？凭**判据是「测试集合不随并行度变化」**，
而不是「依赖逐字节相同」：实测 `-n 2/4/6/8/auto` 与串行的
`passed/skipped` **逐字相同**（2738 / 9）。测试**跑什么**是确定的；只有
**用哪个并行调度器去跑**是浮动但锁了直接版本的。

想连解析也钉死，唯一办法是把 xdist 写进 lockfile——那要连带一次 41 的环境更新（见下）。

### 修订（2026-10-18，同日第二次提交）：`--with` 换掉了

上面那套 `--with` 方案**在被推广前就被一件事推翻了**：把同一改动套用到
health-flow 时，`tests/test_e2e_seed.py` 立刻变红。查下去发现根因不是隔离，
是 `--with` 本身：

```
uv run python -c 'import sys; print(sys.executable)'
    -> <repo>/.venv/bin/python3
uv run --with pytest-xdist==3.8.0 python -c 'import sys; print(sys.executable)'
    -> ~/.cache/uv/builds-v0/.tmpXXXXXX/bin/python
```

**`--with` 不只是多装一个包，它把整条命令放进一个临时覆盖环境执行**，
`sys.executable` 与 `sysconfig` 的 `purelib`/`platlib` 全都跟着改道。于是任何
**拿 `sys.executable` 起子进程、或按 purelib 找 site-packages** 的测试，看到的是
另一个环境 —— 而它会把这件事报成**代码故障**。health-flow 有一处正好依赖 purelib
（脚本模式在 editable install 失效时自举 `import app`），当场就红。

**本仓当时是侥幸没红**（`test_env_41_editable_guard` 用 `sys.executable` 起子进程，
但它只需要一个能读 `PYTHONPATH` 的解释器）。侥幸不是决策，所以改了：

```yaml
- run: uv sync --locked --inexact --dev          # --inexact 才不会被下一步删掉
- run: uv pip install "pytest-xdist==3.8.0" "execnet==2.1.2"
- run: uv run pytest -n auto -rs | tee /tmp/pytest.log   # 不再传 -q，见下
```

装进**项目 venv**，`sys.executable` 保持是项目自己的解释器。

- **必须有 `--inexact`**：不加它，下一句 `uv run` 的隐式同步会把刚装上的 xdist
  删掉（实测 `uv sync --locked --dev` 之后 `import xdist` → `ModuleNotFoundError`）。
- **不用 `uv run --with` 的另一个理由**：它**只有在包不在 venv 里时才**建覆盖环境
  （实测：venv 里已有 xdist 时 `--with` 的 `sys.executable` 是正常的）。
  于是同一个命令在"干净 CI"和"开发者本机装过"两种情况下**行为不同** ——
  这正是 CI 与本地不一致最容易骗人的形状。
- **代价（说清楚）**：`--inexact` 让 venv 可以有 lockfile 之外的内容，
  所以这个 job 的环境**不是** lockfile 的精确复现。判据仍是「测试集合不随并行度变化」。
- **回归钉子**：`tests/test_parallel_setup.py` 钉住「测试解释器 = 项目 venv」
  这条不变量。它**做过反向验证**：干净 venv + `--with` 下 3 条里挂 2 条，
  新配方下 3 条全过。没做过反向验证的钉子只证明自己会绿。
  ⚠️ 它比对的是**未解析的** `sys.executable`：venv 的 `bin/python3` 是指向
  基础解释器的符号链接（`-> ~/miniconda3/bin/python3.13`），先 `resolve()` 会把路径
  移出 venv，让好好的环境也判失败。

### 第三个坑（review 抓出）：这条钉子差点把 AFK 审查链路钉死

第一版的第 3 条断言写的是**无条件** `import xdist`。review 指出：本仓有**三条**
跑全量的路径，只有 CI 装那个包：

| 路径 | 怎么同步依赖 | 有 xdist 吗 |
|---|---|---|
| CI | `uv sync --locked --inexact --dev` + `uv pip install` | 有 |
| `.sandcastle/sandbox-prepare.sh:13` | `uv sync --locked --dev` | **没有** |
| `.sandcastle/review/prompt.md` 第 3 步 | `uv sync --extra dev && uv run pytest` | **没有** |

后两条是 **AFK 审查/修复流程的强制验证步骤**。无条件 import 会让它们在**这个
专门用来阻止静默破坏的改动**里静默变红。这正是本 ADR 通篇在说的那个形状，
只是这次出现在我自己的守卫上。

改法：条据**这次运行**而不是**意图** —— xdist 会在每个 worker 里设
`PYTEST_XDIST_WORKER`，所以条件写成「只有带着 worker 标记却没有 xdist 才是错」，
其余情况 skip（且在串行时 skip 是**正确的**，不是掩盖）。

三种模式实测：

| 场景 | 结果 |
|---|---|
| 串行 + 装了 xdist | 3 passed |
| `-n auto` | 3 passed |
| 无 xdist + 串行（AFK 沙箱） | **2 passed, 1 skipped**（之前是 1 failed） |

### 同一轮修掉的第二个坑：`-qq` 吞掉汇总行

CI 的 Tests 步骤原来写 `uv run pytest -q -n auto`。`pyproject.toml` 的 `addopts`
**已经是 `-q`**，再加一个就是 `-qq`，而 **`-qq` 会把 "N passed, M skipped" 汇总行
整个吞掉**。上一版 PR 里 CI 日志没有汇总行、只能靠数进度点确认跑了多少测试，
根因就是这个。

现在这一步不止断言"绿"，还断言**量**，因为三件事都能让绿变得没有意义：
收集为空（import 错误吞掉整个目录，pytest 仍以 0 退出）、worker 崩掉漏跑、
测试静默变成 skip（**记成 pass 的 skip 是假绿**）。所以要求有汇总行、
`passed ≥ 2700`、`skipped ≤ 10`，并用 `-rs` 打印每条跳过的原因。

**基线是 CI 的值，不是本机的值**（这条差异在第一次 CI 跑时就撞上了）：

| | passed | skipped |
|---|---|---|
| 本机（开发机） | 2741 | 9 |
| CI（GitHub-hosted） | **2740** | **10** |

多出来的是 `test_cd_deploy_scripts.py` 的 dry-run：它需要 `dev-host`，而那东西只在
开发机上。断言第一版写死 `<= 9`，CI 立刻红 —— **这条断言在第一次真实运行时就抓到了
一个量差**（虽然这次是良性的）。而 `-rs` 打印的原因让「多出来的是有意的环境跳过」
一眼可判；换成一个只有计数的断言，这里就只能靠猜，或者把阈值调到"反正不会红"。

四条负向对照都真跑过：空收集 / 只跑 5 个 / 跳过 20 个 / 正常，全部按预期拦下或放行。

> 顺带纠正上一版 ADR 里的一句话：它说 `-n 8` 那次 flake「已消失」。
> 本仓范围内是对的（隔离确实修掉了它），但把它当成"`--with` 方案可用"的证据
> 是错的——真正推翻 `--with` 的是 health-flow，不是这里。

## 结果

同一台机（8 核 / 15 G），同一份代码：

| 配置 | 耗时 | 结果 |
|---|---|---|
| 串行 | 214.3 s | 2738 passed / 9 skipped |
| `-n 2` | 110.9 s | 2738 passed / 9 skipped |
| `-n 4` | **69.7 s** | 2738 passed / 9 skipped |
| `-n 6` | 58.8 s | 2738 passed / 9 skipped |
| `-n 8` | 64 s（3 连跑一致） | 2738 passed / 9 skipped |
| `-n auto`（=8） | 63–66 s | 2738 passed / 9 skipped |

**每个并行度的通过/跳过数与串行逐字相同**（2738 / 9），这满足判据 ①。

`-n 8` 三次都干净。ADR 0037 时代 `-n 8` 出现过一次
`test_an_unknown_analysis_is_not_found`——该文件（`tests/test_ops_analysis_api.py`）
确实调 `FAQCatalog.bundled()`，所以那次 flake 与本记录修的是同一个根因，现已消失。
`-n 8` 没比 `-n 6` 更快，因此 **CI 用 `auto`（本机 8 核，不强求更高并行度）**。

## 被否掉的替代形态

- **把 `pytest-xdist` 加进 `pyproject.toml` 的 dev 依赖。** 这是最自然、最可发现的写法，
  也是我第一个想到的。它输在**依赖漂移门**上：`uv.lock` 一变，CD 拒绝部署，
  直到人工更新 41 的环境。一个测试提速不该连带一次生产环境变更。
  代价是 `--with` 每跑一次要重解析一次包元数据（实测开销可忽略），
  以及 xdist 版本要在 workflow 里显式维护。

- **把旧测试改成用 `monkeypatch` 打补丁改路径。** 工具的路径由模块级常量
  （`CATALOG`、`RECOMMENDATIONS`、`WIDE_TABLE`）派生，而测试是 `subprocess` 调用它。
  打补丁要跨进程边界，做不到；改工具去接受路径参数，则是**为了测试改生产接口**。
  复制整棵树让工具原样工作。

- **用 `pytest-xdist` 的 `--dist=loadfile` 只保证文件级隔离。** 那是**另一个方向**：
  它让同一个文件内的用例落在同一个 worker，而本问题的读者在**别的文件**里。
  文件级分组完全挡不住跨文件的读者-写者竞争。

- **靠 `--reruns` 掩盖。** 竞争窗口几十毫秒，重试能让红灯变绿而不修任何东西
  ——0037 已经点名这是「把真 bug 变成噪音」。

- **`-n 8` 以上。** 实测不更快（8 核已饱和），且内存余量只剩约 1 G，再高是在赌。

- **只钉 `pytest-xdist`，不钉 `execnet`。** PR 的第一版就是这样，review 指出
  `execnet>=2.1` 仍是无约束解析。想用「反正 xdist 版本固定了」来省掉第二个钉子，
  就是把确定性寄托在一个未声明的传递依赖上。

- **`uv run --with pytest-xdist …`（第一版，已换掉）。** 见上文「修订」一节：
  它会把整个进程放进临时覆盖环境，`sys.executable`/`purelib` 一并改道。
  它在本仓**看起来是能用的**，这才是它危险的地方 —— 一个只在别的仓才炸的机制，
  在这里会一路绿灯通过 review。

## 后果

- **全量 218.65 s → ~65 s**（相对最初 299.62 s 是 **4.6×**），CI 的 Tests 步骤同受益。
- `tests/test_merge_faq_i18n_tool.py` 不再依赖仓库作为暂存区：**跑全量后工作树是干净的**
  （之前会短暂把资产改脏）。这是本记录顺带的正确性收益，不只是速度。
- ADR 0037 里「并行被撤回」与「下一轮判据」两节已就地更新，指向本记录。
- **代价（说清楚）**：每个 worker 各复制一份模板树（13 ms/用例），
  以及 xdist 成为一个**需要在 workflow 里手动跟进版本**的依赖——它不在 lockfile 里，
  所以 `uv sync` 不会替你升级或审计它。这是为了不碰生产依赖而付的钱。
- **未解决**：`test_caller_auth_mapping.py`（26 s）、`test_cd_deploy_scripts.py`（26 s）
  等文件是**真的在干活**（真实子进程、真实验证），不是固定等待，本记录不动它们。
  0037 与本文都没有把「减少测试」当提速手段。
