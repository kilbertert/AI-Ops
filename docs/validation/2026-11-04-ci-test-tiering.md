# 2026-11-04：CI 测试分档 —— 文档提交不再跑全量

**里程碑目标**：全量测试每次提交要 192 秒，占 CI 整个 job 的 86%。但最近 40 个提交里
有 15 个只改 `docs/`。打掉这部分浪费，**不删测试**。

## 已完成内容

`ci.yml` 新增一步 `Decide the test tier`，在跑测试前判断这次 PR 改了哪些文件：

- 改动**全部**落在 `docs/` 或根层 `*.md` → 只跑"读文档"的那批（实测 **738 条 / 32 秒**）
- 其余任何情况 → 全量（**4141 条 / 192 秒**）

降级档的测试集合**不写死文件名**，按"测试源码里提没提到文档"现算，新增一个读文档的
测试会自动进来。集合算空时退回全量，而不是让它在 0 条上绿。

判定写成「**默认全量，白名单才降级**」。反过来（默认降级、列举需要全量的路径）会在
漏配时静默少测 —— 这个仓已经踩过一次同类：`ci.yml` 顶部记着"path filter 让改 README
的提交绕过了一条读 README 的测试"。

## 为什么不是删掉全量测试

删掉是唯一一个**在没人看的时候会静默退化**的选项：`verify` 是保护 main 的必需检查
（ruleset 23760870），删掉测试它就只剩静态检查，等于把"合并进 main 的代码跑没跑过测试"
完全交给作者自觉。而分档的错误形态是**多跑三分钟**，不是假绿。

## 验证结果

新增 `tests/test_ci_test_tier.py`（15 条），**抽取 `ci.yml` 里那段判定脚本原文执行**，
不是复制逻辑：

| 输入 | 期望档 |
|---|---|
| 纯 `docs/*.md`、根层 `README.md` / `SOP.md` | docs |
| `src/**`、`tests/**`、`pyproject.toml`、`ops/**`、`deploy/**` | full |
| `docs/a.md` + `src/x.py`（混合） | full |
| `docsomething.py`、`sub/docs/a.md`、`sub/README.md` | full |
| 空 diff / 取不到 PR base | full |

**反向验证**：把判定正则的两个 `^` 锚点去掉，`sub/docs/a.md` 与 `sub/README.md` 两条
立即转红 —— 其中 `sub/docs/a.md` 是真漏洞（第一版确实放过了它），是这套测试抓出来的。

## 复核轮追加（Codex Review 一条）

**发现成立**：选取正则只认 `docs/…` 这个字面量，而用 pathlib 拼路径的测试
（`PROJECT_ROOT / "docs" / "agents" / "company-code-baseline.md"`）源码里没有这个字面量，
被留在档外 —— 改这些文档时 CI 是绿的，守着它们的测试一条都没跑。实际漏了 5 个文件：
`test_company_entity_map.py`、`test_company_auth_scope.py`、`test_operator_questions_triage.py`、
`test_recon_procedure_evidence.py`、`test_repo_index_evidence.py`。

修法与分档同一条原则：**不抄文件名清单**。正则补一支 `"docs" */`（`grep -E` 与 Python
`re` 都认的写法），并新增 `test_the_docs_tier_selects_every_test_that_reads_docs`
—— 它从 `tests/` 里**现找**用 `"docs" /` 读文档的文件，逐个要求被选取正则命中。
删掉正则里那一支，这条测试列出全部 5 个漏掉的路径并转红（已实测）。

改正后的量：文档档 **45 个文件 / 738 条 / 32 秒**（原记 683 条 / 26 秒）。

全量：`4141 passed, 9 skipped in 204.95s`，`ruff check` / `format --check` 通过。

## 已知缺口

- 降级档只覆盖"读文档"的测试。若某天新增一条**必须**在文档改动时跑的测试却在源码里
  没提 `docs/`，它会被漏掉。判据是"提到就进档"，这是一条启发式，不是完备性证明。
- 分档只在 `pull_request` 触发时生效；直接推 main 或手动触发一律全量。

**真实故障业务验收：未完成** —— CI 流程变更，无用户可见行为变化。
