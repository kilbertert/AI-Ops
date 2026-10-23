# 112. 版本号由 `VERSION` 文件显式决定，CD 只读它

日期：2026-10-23
状态：已采纳
影响版本：`VERSION`（新增）、`.github/workflows/cd.yml`、`deploy/deploy-41.sh`、
`tests/test_release_version.py`（新增）

## 背景：Release 没有版本号

ADR 0101 把 `CHANGELOG.md` 挪到发布时生成，并如实记下一个限制：本仓从未打过版本
tag，所以生成的备注只有一个 `Unreleased` 节 —— 它回答「哪些改动已落地」，不回答
「上一次部署里有什么」。

实际状态：

- `pyproject.toml` 的 `version` 与 `src/aiops_diagnostics/__init__.py` 的
  `__version__` 都是 `0.1.0`，**从未被推进过**，且两处**各写各的**
  （`deploy-41.sh` 的注释原文：「与 `__init__.py` 本就不同步」）；
- 部署时把 `__version__` 改写成 `0.1.0+<short-sha>`，`/health` 据此自证；
- Release 的 tag 是 `release-<sha12>`，不是版本。

所以「这次发布是什么版本」这个问题，此前没有答案。

## 决定

### 一、`VERSION` 是权威，人改它来发布

仓库根新增 `VERSION`，内容为裸 semver（`0.1.0`）。**只有人改它。** CD 读它打 tag。

### 二、CD 不从提交推版本

被否的路线（见下）包括「按 Conventional Commits 自动推」。理由不是实现麻烦，而是
**归属**：版本号是「这次发布包含什么」的声明，自动推导等于让提交信息的措辞决定
发布边界 —— 一次误标的 `feat!` 会跳大版，而没有人做过那个决定。

### 三、一份权威，两份副本，由测试钉住

`VERSION`、`pyproject.toml` 的 `project.version`、`__init__.py` 的 `__version__`
必须相等，`tests/test_release_version.py` 强制。此前 `pyproject` 与 `__init__`
各写各的、从未同步，正是「两处独立写同一个事实」的典型失败：它们不会同时被改，
某天 `/health` 报的版本与发布 tag 说的版本不是一回事，而两边都看起来正常。

`deploy-41.sh` 另加一道运行时断言：`__version__` 与 `VERSION` 不一致就停止部署，
而不是把一个过期版本打上生产。

### 四、版本 tag 只打一次，且永不移动

- **版本 tag**：`v<version>`，指向**该版本首次部署**的 commit。
- **部署记录**：仍是每次部署一个 Release，tag `release-<sha12>`。

**为什么版本 tag 不再带 `+<sha>`**：实测 2026-10-23，`dev-changelog` 按版本 tag 分节，
若每次部署都打 `v0.1.0+<sha>`，changelog 会**一次部署一节** —— 那正是版本号要消除的
碎片化。版本 tag 是「一个发布」的标记，不是「一次部署」的标记。

**为什么已存在的版本 tag 不被移动**：一个会往前走的 tag 不是版本，是一个在所有人
记录它之后继续变化的指针。同版本再次部署时，步骤会打印现有 tag 指向哪个 commit，并
提示「要发新版本就 bump `VERSION`」。

## 备选方案与取舍（被否的路线）

- **按 Conventional Commits 自动推版本**。被否：见「决定·二」，是归属问题不是工作量。
- **每次部署打 `v<version>+<sha>` tag**。被否：实测会让 `dev-changelog` 一次部署开
  一节，changelog 变成按部署切碎的流水。
- **不加版本号，只在 Release 标题里写 commit 区间**。被否：Release 仍不可按版本引用，
  `dev-changelog` 仍只有一个 `Unreleased` 节，下一个人还会再问一次。
- **把 `__init__.py` 与 `pyproject.toml` 合并成一处**。被否：打包元数据与运行时模块
  是两件东西（一个进 wheel，一个在 import 时读），合并会把它们耦合得更紧；三处相等
  由测试保证，比强行合并代价小。

## 后果

- 发布备注开始按版本分节：`v0.1.0` 之前的进 `[0.1.0]`，之后的新提交进 `[Unreleased]`。
  这是实测过的行为（临时仓库里 `dev-changelog` 的输出已验证）。
- 首次部署后仓库会多一个 `v0.1.0` tag，指向那次部署的 commit。
- 三处版本不一致会让 `verify`（必需检查）变红，而不是等到生产自报错误版本时才发现。
- **仍未解决的**：本次只建立机制，**没有推进版本号**。`0.1.0` 之前的 664 条 changelog
  条目全部落在 `[0.1.0]` 一个节里 —— 历史没有分版本，也不该由这次改动凭空编造。
