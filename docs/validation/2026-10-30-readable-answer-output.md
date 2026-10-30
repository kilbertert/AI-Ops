# 回答正文可读排版：本地验证记录

日期：2026-10-30（UTC）。分支`fix/readable-answer-output`（PR #1175），基线`bf6085eb`，
合并 commit `b9e6129b`，换号与内容锁 commit `3e487437`。本记录是**提交前**的本地验证，
远端 CI、合并与部署结果不在本记录内，另按交付流程记录。

本切片只解卡既有实现（`ab4c9503`），不新增文案语义。设计取舍见
[ADR-0179](../adr/0179-answers-are-plain-text-laid-out.md)，
目标与缺口见[开发进度记录](../开发进度/2026-10-30-readable-answer-output.md)。

## 环境与范围

开发机、本任务 worktree（`.worktrees/AI-Ops-readable-answer-output`）、本地 Docker。
未访问 41、未连真实 Dify、未调用真实模型、未改任何生产数据或配置。
镜像在本地重建，未推送任何注册表。

## 自动化检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check .` | 通过 |
| 格式检查 | `uv run ruff format --check .` | 通过，1107 files already formatted |
| 相关测试 | `uv run pytest tests/consumer tests/operations tests/tooling tests/test_readme_architecture.py -rs` | **965 passed / 2 skipped**，69.88s |
| ADR 结构 | `node .sandcastle/adr.check.mjs --base origin/main` | ok，169 records，0 draft |
| 差异空白 | `git diff --check origin/main` | 通过 |

两条 skip 都是既有环境跳过，与本次改动无关：

- `tests/operations/test_operator_catalog_vi_mn.py:48` —— operator 目录已覆盖全部语言（#565）；
- `tests/operations/test_operator_questions_triage.py:200` —— 未配置 `AIOPS_GL_CA`，跳过需要
  公司 GitLab 的复核。

## 失败与修复（按发生顺序）

1. **合并冲突（README、`deploy/m2-live-03/images.lock.json`）**。README 按 main 的归包结果
   取平后漏掉本分支新增的 `consumer/consumer_time.py`；补回「C端业务」一行。
   首轮我选择保留 main 的镜像身份不动，结果下一条测试立刻把这个选择判红。
2. **`test_gateway_context_digest_is_reproducible_and_matches_the_lock` 失败**：
   `实际 90e54f83… / 锁 e3ff5c96…`。根因是合并后树变了。按 ADR-0163 的既有路径用
   `build-image.sh --update-lock` **真实重建**写回，而不是只改摘要字段 ——
   手工改 `source_sha256` 会让它与 `image_id` 脱钩，preflight 在部署宿主上必然失败。
   **重建后该测试转绿，工具链专项 18 passed。**
3. **构建脚本写 `identity.json` 时 `mkdir /opt/aiops-m2-live: Permission denied`**。
   `M2LIVE_DIST` 默认指向 `/opt`，开发机上需显式改到可写目录
   （本次 `M2LIVE_DIST=$PWD/dist/gateway-image`，该目录 gitignored）。这不改变锁的内容，
   只影响身份文件落在哪。
4. **基础镜像取不到 RepoDigest**：`command -v docker` 通过但
   `docker image inspect -f '{{index .RepoDigests 0}}' python:3.13-slim` 为空时脚本会拒绝构建
   （设计如此，避免拿本地构建的替身当基准）。改为按摘要引用
   `python@sha256:70729b46…` 后通过 —— 该摘要与锁里原有 `base_digest` 相同，
   说明基准没换。

## 制品身份

| 项 | 值 |
| --- | --- |
| 镜像 | `aiops-m2-live-03-gateway:readable-output-merged`（仅本地） |
| `image_id` | `sha256:61031572485dabe518a0eac3b9f0db4661965f54ea38bf01f94f56a6b529ef45` |
| `source_sha256` | `90e54f83f9eaa33ffb65c87e0db3a78d35fdac1b3f76996a14e3e791d37ffab5` |
| `uv_lock_sha256` | `5c2eb20c285dcbcdfc21b2449da7a562bf8c74415bb1708451149ebf7cacdb6f` |
| 基础镜像 | `python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f` |
| `built_at`（UTC） | 2026-10-30T04:00:22Z |

## 未完成项（不得读作通过）

- **未完成业务验收。** 没有真实账号、真实订单或 App/H5 上的回读证据；
  本记录的绿色只说明代码与排版渲染在本地成立。
- **真实前端渲染未验**：`blocks[]` 的纯文本排版在真机/小程序上的观感（换行、空行、
  `【】` 小标题、编号对齐）只能靠人眼确认，本地断言只看结构特征。
- **产品诉求里的「下结论 + 通俗注释」不在本切片范围**，见开发进度记录的「已知缺口」。
  本切片不宣称回复已经「像人」「商务看得懂」。
- 远端 CI、合并、部署与灰度均未开始。
