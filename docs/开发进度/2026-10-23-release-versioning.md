## 2026-10-23 · 版本号由 VERSION 文件决定

**目标/分支**：`feat/release-versioning`，基线 `429236d`（#1033 后的 main）。

### 一、解决什么

ADR 0101 留下的限制：Release 没有版本号，`dev-changelog` 只产出一个 `Unreleased` 节。
根因是版本号从未被当作发布边界 —— `pyproject.toml` 与 `__init__.py` 都停在 `0.1.0`
且各写各的。

### 二、改法

- 新增 `VERSION`（裸 semver），**人**改它来发布；CD 只读它
- CD 首次部署某版本时打 `v<version>` tag，**此后不移动**；每次部署仍建 `release-<sha12>`
- 部署前断言 `__version__ == VERSION`，不一致就停
- `tests/test_release_version.py` 让三处声明保持一致

### 三、关键取舍

**版本 tag 不带 `+<sha>`**：实测若带，`dev-changelog` 会一次部署开一节。
**不从 commit 推版本**：版本号是「这次发布包含什么」的声明，自动推导等于让提交信息的
措辞决定发布边界。

### 四、判据

`pytest tests/test_release_version.py` → 4 passed；四处变异全部转红。

### 五、待验证

**`v0.1.0` tag 要等下一次部署才会真的出现。** 本 PR 只建立机制，`VERSION` 仍为
`0.1.0`，历史 664 条 changelog 条目全部落在 `[0.1.0]` 一节 —— 它们从未被分过版本，
不凭空编造。
