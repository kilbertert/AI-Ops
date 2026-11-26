# ADR-0356: 退役 6 个 `#475–#480` 公司源码探查历史过程文档测试

Status: Accepted
Date: 2026-11-26

## Context

2026-10-01 在完成公司侧源码只读探查基线（`#414`）时，仓库新增了一组共 6 个独立过程取证测试（`#475–#480`，合计 57 条用例、1,387 行测试代码）：

- `tests/integrations/test_recon_procedure_evidence.py`（`#475`，8 条）
- `tests/integrations/test_repo_index_evidence.py`（`#476`，7 条）
- `tests/integrations/test_company_entity_map.py`（`#477`，8 条）
- `tests/integrations/test_company_auth_scope.py`（`#478`，10 条）
- `tests/integrations/test_l4_order_authorization.py`（`#479`，11 条）
- `tests/operations/test_operator_questions_triage.py`（`#480`，13 条）

依赖审计显示：
1. 这 6 个文件**零导入且零覆盖** `src/aiops_diagnostics/` 任何现役运行时代码，仅校验 2026-10 探查阶段生成的 Markdown 段落措辞、旧表行数与已归档阶段文本；
2. 它们同时配置了依赖公司内网 GitLab 身份锚点 `AIOPS_GL_CA` 的 `@pytest.mark.skipif`，在无内网锚点的正常本地与 CI 环境下长期贡献了全量测试 **9 条固定 `skipped` 中的 8 条**——而 `tools/verification/ci_results.py` 强制门禁为 `skipped <= 10`，极度挤压真实可跳过的安全边界；
3. 移除这 6 个文件后，`.github/workflows/ci.yml` 的 `docs` 档仍剩余 **40 个文件、643 条用例**，远高于 `floor=400` 门禁（原有 46 文件 / 700 用例），不会影响其他文档与契约测试。

## Decision

整批安全退役上述 6 个 `#475–#480` 历史过程取证测试（共 `-57` 条用例、`-1,387` LOC），并同步更新关联文档：

1. 删除 `tests/integrations/test_recon_procedure_evidence.py`、`test_repo_index_evidence.py`、`test_company_entity_map.py`、`test_company_auth_scope.py`、`test_l4_order_authorization.py` 与 `tests/operations/test_operator_questions_triage.py`；
2. 保留现役只读工具 `deploy/company-gitlab-api.sh` 与操作手册 [company-code-recon-procedure.md](../agents/company-code-recon-procedure.md)，并将手册 §6 末节与 [repository-layout.md](../architecture/repository-layout.md) 同步更新为已依本 ADR 退役的准确状态。

## Alternatives considered

### 1. 给这 6 个文件打上新 marker 并默认过滤，但不删除源码
- **最强论点**：保留对 2026-10 探查 Markdown 文本格式的机械断言。
- **否决原因**：`#475–#480` 守护的是已封存的一次性调研排版与固定表计数，而非现役业务或代码；继续保留需长期维护其引用同步（如 ADR-0346 已曾为此单独修补 `test_l4_order_authorization.py`），且不跑 `AIOPS_GL_CA` 时永远固定占满 `skipped` 预算。直接退役并保留历史 Git 记录最干净。

## Consequences

- 全量测试集清理 **-6 个文件 / -57 条用例 / -1,387 LOC**，且无任何 `src/` 业务覆盖损失；
- 常规环境全量运行的永久 `skipped` 用例由 **9 条降至 1 条**，彻底释放 `ci_results.py`（`skipped <= 10`）的缓冲余量；
- CI `docs` 档保持 40 个文件 / 643 条有效断言，满足 `floor=400`。
