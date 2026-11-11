# llmdoc V3 知识检索面初始化

目标：按 `/llmdoc:init` 为仓库建立 V3 `llmdoc/` 检索面——稳定文档路由图（`.mdx` 带
`description`/`kind`/`code.paths`/`relations` 前言），让 agent 在广域探索前先按概念
与文件路径路由到属主文档。
基线 `e60a6049`；分支 `docs/llmdoc-bootstrap`；工具 `@tokenroll/llmdoc` 3.6.1
（经 `npx -y` 缓存执行，未加入 package.json 或 lockfile）。
三个提交依次进入本分支：`e5e952e1`（目录登记、忽略规则、README 与 ADR-0245）、
`b2eb5022`（meta.json 指纹定稿）、`4d541ccb`（19 篇 `.mdx`）。
本里程碑不推送、不开 PR，合并与否由审查闭环决定。

已完成：

- 19 篇文档：根文档 `execution-model.mdx`（仓库总合同）加 10 个主题目录——
  gateway×2、identity、execution×2、common、consumer、operator、conversation、
  analytics、publication、dify×3、knowledge、deployment、governance×2。
- 领域/属主矩阵先行：每个一等子系统拿到属主文档或显式 no-doc 决定；
  调查员边界合并过两轮（gateway 9 簇→5 篇、dify/knowledge→4 篇）。
- 四份调查报告与合并矩阵在 `.llmdoc-tmp/`（本机 gitignore），不进台账。
- [ADR-0245](../adr/0245-llmdoc-knowledge-surface.md) 记录位置契约（`llmdoc/` 只能
  在仓库根）、`docs/` 与 `llmdoc/` 分工、`.llmdoc-tmp/` 忽略、meta.json 只能经 CLI 管理。
- `tests/test_repository_layout.py`、`docs/architecture/repository-layout.md`、
  `README.md`、`.gitignore` 同步登记新顶层目录。

验证：`llmdoc validate` 通过；Context Floor 验收全绿——15/15 概念探针首个结果即预期
属主、约 28 个文件路由探针全部命中、零 glob、`tree --docs` 与 requires 闭包核对一致。
布局/README/退役三类守卫测试通过；ADR 范围检查 3 提交通过（233 条记录）。
完整判据与结果见[验收记录](../validation/2026-11-11-llmdoc-bootstrap.md)。

当前缺口：README.md:184 遗留一处 `site_note_review` 失效提及（已标记，未在本轮修）；
Dify 工具提供方 `api_key_header_prefix` 坑仍只存在于扁平档案，未收入检索面；
检索面质量未经真实任务回用验证。
未完成业务验收：本里程碑为文档/工具面，无业务旅程适用。

操作知识自检：本轮只有本地 `npx` CLI、`git` 提交与 `pytest`，无远端运维命令；
`llmdoc` 工具面的位置契约与台账纪律已在 ADR-0245 登记。
