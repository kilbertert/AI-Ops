# 370. 归档已被取代及局部战术 ADR 并建立现行 ADR 分域索引

日期：2026-11-28
状态：已采纳；依据并延续 [ADR-0345](0345-offload-legacy-receipts-to-archive-repo.md) 的私有归档仓机制，完成主仓库 ADR 历史债务治理收敛。

## 背景

1. **存量噪音过高**：主仓库 `docs/adr/` 累计至 355 份记录（约 1.39 MB），其中混合了大量已被后续重构/退场决策明确取代、反转、撤回或到期的历史文档，以及面向单次 PR 修复、单轮部署调整的局部战术 ADR。新接手工程师（尤其是理解 Dify 与多环境接入链路时）难以一眼分辨哪些决策仍在现行生效、哪些方案早已被推翻。
2. **前置基础设施已就绪**：[ADR-0345](0345-offload-legacy-receipts-to-archive-repo.md) 已建立带 `MANIFEST.sha256` 校验的独立私有归档仓库 [`kilbertert/AI-Ops-history-archive`](https://github.com/kilbertert/AI-Ops-history-archive)，而 PR #1809–#1817（[ADR-0365](0365-dify-web-acr-oci-distribution.md)–[ADR-0369](0369-retire-legacy-dify-edge-bundles-and-add-request-tracing.md)）已将 Dify @ 36 运行链路收敛为唯一架构入口 [`deploy/dify-36/ARCHITECTURE.md`](../../deploy/dify-36/ARCHITECTURE.md)。

## 决定

1. **按「架构现行有效」标准筛选并归档 231 份历史/局部 ADR**：
   - 将 **231 份**（约 819.3 KB）已被取代、反转、撤回、到期、已退役路径的记录，以及仅记录单次 PR / 局部修复 / 一次性清理的战术 ADR，原样（逐字节）归档至 [`kilbertert/AI-Ops-history-archive`](https://github.com/kilbertert/AI-Ops-history-archive/tree/4bed22cbabbcbe80f242c83275a46d3f550f996d)（源修订 `b4aa1c1e5e7ce10628bcc339894611f92cc67939`，归档提交 `4bed22cbabbcbe80f242c83275a46d3f550f996d`，纳入 `MANIFEST.sha256` 与 `ARCHIVE_LIST.json` 批次二校验后从主仓工作树移除）。
   - 主仓库 `docs/adr/` 收敛为 **125 份现行核心决策记录**（原 124 份现行架构/边界/契约 ADR + 本记录 `ADR-0370`），减少约 65.1% 的存量文件数。
2. **建立分域现行索引 [`docs/adr-index.md`](../adr-index.md)**：
   - 按「核心边界与身份」「Dify @ 36 架构与侧边车」「Gateway / Schema / 节点与健康报告」「C 端客服与知识库」「管家端与追踪」「多环境开通与 OCI 分发」「仓库治理、测试与归档」七大领域编排全部 125 份现行 ADR，并给出「Dify @ 36 现行决策 vs 已退役旧路径」对照速览。
   - 在 [文档导航](../README.md)、[Dify @ 36 唯一架构入口](../../deploy/dify-36/ARCHITECTURE.md) 与 [ADR 约定](../../docs/agents/architecture-decisions.md) 中将其列为首选检索入口。
3. **编号水位线永久递增、不回用已归档编号**：
   - 全部 231 个已归档编号（0003–0359 区间内被归档序号）永久保留给归档仓历史引用，**禁止复用**，也不做重编号。
   - 主仓库后续新增 ADR 一律从 `0371` 起顺延递增。
4. **链接闭环与零破坏策略**：
   - 现役文档、保留 ADR 与保留 `docs/validation/*.md` 回执中引用到本次已归档 ADR 的相对链接，统一改写为固定指向归档提交 `4bed22cb` 的 GitHub permalink，保持历史事实不被改写且全仓 Markdown 相对链接零断链；将 `docs/adr-index.md` 纳入 `tests/test_readme_architecture.py` 链接校验。
   - 不修改 `.sandcastle/adr.check.mjs`（其按当前工作树现存文件扫描覆盖与结构门禁）。

## 备选方案

- **方案 A（只加目录索引，不从主仓删除 231 份历史/局部 ADR）**：被否。虽然阅读有了导航，但全仓文本检索、CI 文档浏览与新接手工程师列目录时仍会被三百余份文件混淆；此前评估已指出“存量 355 份 ADR 未做大面积删减/归档”正是未关闭的认知成本残余。
- **方案 B（仅保留在 Git 历史，不写入 `AI-Ops-history-archive`）**：被否。违背 [ADR-0345](0345-offload-legacy-receipts-to-archive-repo.md) 已确立的“独立私有归档仓库 + `MANIFEST.sha256` 可审计校验”约定，也不利于从保留文档/回执点开固定 permalink 溯源。
- **方案 C（连同现行被引用的设计文档或测试/脚本直接锚定的 ADR 一并归档）**：被否。会把现役架构边界（如 OAuth2/UPMS 权限边界、Dify 边缘架构 ADR-0365–0369、Schema v4、CI 分层 ADR-0355/0357）清出工作树，破坏工程规范“代码答运行、测试答约束、ADR 答权衡”的最小完备性。

## 后果

- **收益**：`docs/adr/` 由 355 份降为 125 份（-64.8% 含本记录，原存量归档 65.1%），新人从 [`docs/adr-index.md`](../adr-index.md) 即可在分钟级看清现行生效架构真源，不再被已推翻方案干扰。
- **代价**：极少数历史细节需通过 [`docs/adr-index.md`](../adr-index.md) 中的归档仓链接或 `git log -- docs/adr/` 查阅；新增 ADR 作者须遵守 `0371+` 水位线（在 [architecture-decisions.md](../agents/architecture-decisions.md) 与 [adr-index.md](../adr-index.md) 明示）。

## 验证

- 在 `kilbertert/AI-Ops-history-archive@4bed22cbabbcbe80f242c83275a46d3f550f996d` 运行 `sha256sum -c MANIFEST.sha256` 全部 973 项通过（含本批 231 份 ADR，并与源仓逐字节核对一致）。
- 在主仓库核验 `docs/adr/` 共 125 份文件全部被 [`docs/adr-index.md`](../adr-index.md) 索引，全仓零相对 Markdown 断链；运行 `node .sandcastle/adr.check.mjs --all`、`node .sandcastle/adr.check.mjs --base origin/main`、`node .sandcastle/policy-check.mjs commit`、`tests/test_readme_architecture.py` 与 `tests/test_repository_layout.py` 全通过。
