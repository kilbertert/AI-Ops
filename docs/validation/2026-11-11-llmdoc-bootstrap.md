# 验证：llmdoc V3 知识检索面初始化

日期：2026-11-11；基线 `e60a6049`；候选分支 `docs/llmdoc-bootstrap`。
工具 `@tokenroll/llmdoc@3.6.1`（npx 缓存执行，未登记为项目依赖）。
调查材料目录：`.llmdoc-tmp/`（本机忽略目录），含 4 份调查报告
（gateway-core-identity-execution、business-loops、dify-knowledge、
delivery-services-governance）与合并后的 `domain-owner-matrix.md`。

## 结构性检查

| 命令/入口 | 结果 | 证据 |
|---|---|---|
| `npx -y @tokenroll/llmdoc init-state` | 通过；meta.json 落为 `llmdoc.meta/v3`，baseline=e60a6049 | llmdoc/meta.json |
| `npx -y @tokenroll/llmdoc validate` | ok。期间修掉 identity-scope.mdx 两处 `mdx.syntax.forbidden`：正文花括号集合写法改反引号 | validate 终态输出 |
| `npx -y @tokenroll/llmdoc commit --all -m "docs: bootstrap llmdoc"` | 校验+指纹+提交 meta.json → `b2eb5022` | meta.json 19 条 validatedRevision=e5e952e1 |
| `git add llmdoc/ && git commit` | 19 篇 .mdx → `4d541ccb`（`commit --all` 只提交 meta.json，未跟踪的 .mdx 需单独 add） | git log |
| `uv run pytest tests/test_repository_layout.py tests/test_readme_architecture.py tests/test_runtime_retirement.py -q -n 0` | 全部通过，退出码 0 | 终态输出 |
| `node .sandcastle/adr.check.mjs --base origin/main` | ok；3 提交范围检查，233 条记录，0 draft 豁免 | adr 检查输出 |

## Context Floor 验收（检索面实测）

- **概念路由**：15/15 探针首个结果即预期属主。覆盖：只读边界、scope_fingerprint、
  chat_data_grant、operator 工具声明、dify trial draft、workspace 解析码、
  knowledge_id 语法、节点能力心跳、deploy-41 节点闸、pytest 双槽、ADR 编号、
  health report 作业、FAQ、conversation turn 等。
- **文件路由**：约 28 个代表性文件全部命中属主文档。含一处诚实的多属主路由：
  `gateway_api.py` → gateway-runtime / identity-scope / execution-delegation 三篇。
- **Glob 精度**：全部 `code.paths` 为精确路径，零 glob，按规则豁免精度探针。
- **关系闭包**：`tree --docs` 输出与 requires 闭包核对一致，例：
  CapabilityReporter.java → execution-node-contract →（requires）execution-delegation
  →（requires）identity-scope。
- **显式缺口**：无未登记的一等子系统。

## 有意不建文档的区域（矩阵决定）

- `tools/inventory`、`deploy/dify-36` 上游副本、ops nginx 配置：权威源已存在
  （tools/README.md、上游 tag、ops/README）。
- `integrations/cpms-java`：供应商补丁制品；恢复路径是
  `docs/diag-query-api-plan.md` 加公司 release 分支。
- `.sandcastle/` 内部：模板管理，权威源是 afk-bootstrap 模板。
- `docs/validation/`、`docs/开发进度/`、`deploy/manual-deploys.md`：append-only 台账。
- 路由表、settings 清单、DDL、工具清单、语言清单：可由 manifest/源码重建。
- `tools/experiments/m2_close`：一次性但永不删除（唯一真实 Java harness），
  记入 test-architecture.mdx 正文，不单独成文。

## 未验与交接

未完成业务验收：本里程碑为文档/工具面，无业务旅程适用；检索面质量待后续真实任务回用。
已知未修项：README.md:184 遗留 `site_note_review` 失效提及；
Dify 工具提供方 `api_key_header_prefix` 坑（默认 `basic` 会把 Bearer 组装成
`Basic Bearer`，调用时才 401）已收入 `llmdoc/knowledge/knowledge-plane.mdx`。
分支未推送、未开 PR。
