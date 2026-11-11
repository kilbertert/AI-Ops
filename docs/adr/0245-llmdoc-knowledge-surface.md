# 245. llmdoc 检索面落在仓库根，docs/ 继续承载证据与手册

Status: Accepted

本仓引入 `@tokenroll/llmdoc` 维护的 agent 检索面：根目录 `llmdoc/` 下按主题存放 `.mdx`
文档，`llmdoc/meta.json` 是由 CLI 独占维护的有效性台账。它面向 agent 检索——概念搜索、
变更文件的 owner 路由、先决文档闭包——不面向业务验收阅读。

## 决定与依据

- `llmdoc/` 作为新的一级目录登记进 `tests/test_repository_layout.py` 与
  `docs/architecture/repository-layout.md`。位置与目录名不可移植：CLI 契约固定读取
  仓库根的 `llmdoc/`，放进 `docs/` 下该工具直接不可用。
- `llmdoc/` 只保存稳定知识：决策、边界、不变量、合同与高风险流程；事实的权威仍是
  源码、测试、schema、manifest 与 `docs/`。`code.paths` 只做语义 owner 路由，不追求
  文件覆盖率。
- `docs/` 的角色不变：ADR 记录"为什么是这个形状"、`docs/agents/` 记录操作方法、
  `docs/validation/` 与 `docs/开发进度/` 按一记录一文件留档。llmdoc 文档可以指向这些
  文件，但不复制其内容——重复会在两次编辑之间漂移。
- `.llmdoc-tmp/`（调查底稿、reflection 候选）是本机易失缓存，已加入 `.gitignore`，
  与 `.pytest_cache/` 同性质；正式知识必须进 `llmdoc/`。
- `meta.json` 不接受手工编辑：台账改动只经 CLI（`new`/`adopt`/`mv`/`fingerprint`/
  `commit`），文档基线与修订号由它维护。

## 被否掉的替代形态

- 只靠 `docs/`：它已有 232 篇 ADR 与大量手册，但没有机器可读的路由元数据（检索描述、
  code.paths、前置关系），agent 冷启动仍要全文扫目录才能找到 owner；这正是检索面补的
  缺口，不是再造一份文档。
- 把 `llmdoc/` 放进 `docs/llmdoc/`：目录语义上更整齐，但 CLI 只认仓库根的 `llmdoc/`；
  加一层符号链接会引入"哪个是真实位置"的歧义，比少一层目录更糟。
- 不跟踪、各会话自建：每次会话重建成本高且结果不可共享，与"操作知识必须入仓才算
  共享"（AGENTS.md 操作知识自检）同理；检索面本身就是为跨 agent 共享设计的。

## 后果与验证边界

新增一个受保护的一级目录；目录规则测试与目录规范已同步，理由即本 ADR。
文档体必须过结构 `validate` 与检索验收（概念查询、文件路由、通配符精度），
但这只证明检索面结构可用，不证明其陈述永远新鲜——`meta.json` 修订号与
`/llmdoc:update` 负责后续保鲜。llmdoc 文档出现与 `docs/`、源码冲突时，
以源码与 `docs/` 为准并更新 llmdoc。
