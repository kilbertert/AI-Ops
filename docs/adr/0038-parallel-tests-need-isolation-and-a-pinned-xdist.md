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
uv run --with pytest-xdist==3.8.0 pytest -q -n auto
```

`--with` 把 xdist 装进 uv 的**临时覆盖环境**，不改 `uv.lock`、不改 `pyproject.toml`
——实测跑完两者哈希均未变，`uv sync --locked --dev` 仍然通过。

**版本写死 `==3.8.0`**：`deterministic-ci` 是**不可豁免不变量**，它要求门禁是确定性的。
浮动版本等于让门禁的耗时与行为随上游漂移，所以固定。

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

## 后果

- **全量 218.65 s → ~65 s**（相对最初 299.62 s 是 **4.6×**），CI 的 Tests 步骤同受益。
- `tests/test_merge_faq_i18n_tool.py` 不再依赖仓库作为暂存区：**跑全量后工作树是干净的**
  （之前会短暂把资产改脏）。这是本记录顺带的正确性收益，不只是速度。
- ADR 0037 里「并行被撤回」与「下一轮判据」两节已就地更新，指向本记录。
- **代价（说清楚）**：每个 worker 各复制一份模板树（13 ms/用例），
  以及 xdist 成为一个**需要在 workflow 里手动跟进版本**的依赖——它不在 lockfile 里，
  所以 `uv sync` 不会替你升级或审计它。这是为了不碰生产依赖而付的钱。
- **仍未解决**：`test_caller_auth_mapping.py`（26 s）、`test_cd_deploy_scripts.py`（26 s）
  等文件是**真的在干活**（真实子进程、真实验证），不是固定等待，本记录不动它们。
  0037 与本文都没有把「减少测试」当提速手段。
