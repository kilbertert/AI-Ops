# ADR-0355: Pytest 分层标记与 CI 重型用例路径按需触发

Status: Accepted
Date: 2026-11-26

## Context

[ADR-0354](https://github.com/kilbertert/AI-Ops-history-archive/blob/4bed22cbabbcbe80f242c83275a46d3f550f996d/docs/adr/0354-optimize-test-memory-and-sqlite-speed.md) 解决了 worker 内存膨胀与测试期 SQLite/源码守卫性能问题，但日常开发与多数 PR 仍会在无区分地执行耗时最长的重型用例：

1. **Docker Compose / 本地容器验证与无头浏览器回放占主导长尾**：
   - `tests/dify/test_dify_knowledge_editor_role.py`（23.25 s）、`tests/dify/test_dify_idempotent_import.py`（21.04 s）与 `tests/dify/test_dify36_deploy_assets.py`（中 4 条 `compose` 校验，总计 6 条 `docker` 用例）会启动或调用本地 Docker Compose；
   - `tests/consumer/test_consumer_status_default_path.py::test_offline_drive_replay_passes_all_three_scenarios`（15.52 s，1 条 `browser` 用例）会拉起 Playwright/Chromium 无头浏览器；
   - 7 个 Nginx 入向/出向/TLS 配置测试（60 条 `nginx` 用例）及 27 个 M2/M3 与环境安装/连续性工具测试（493 条 `deploy_tooling` 用例）合计 553 条，在日常仅修改诊断核心业务或路由单测时并非每次都需要完整重跑。
2. **`tools/verification/ci_results.py` 严格限制 `skipped <= 10`**：
   - 若直接用 `@pytest.mark.skip` / `pytest.skip` 跳过重型用例，会直接触发 CI 结果门禁失败；必须采用 `-m` 表达式在收集期 **deselect**，使未触及区域的重型用例不计入 `skipped`。

## Decision

建立无侵入的五类 `pytest` marker 分层与 CI 路径感知路由（P1）：

1. **在 `pyproject.toml` 注册统一分层标记**：
   - `docker`（6 条）：需启动或调用本地 Docker / Docker Compose 的 Dify 部署测试；
   - `browser`（1 条）：驱动 Playwright/Chromium 无头浏览器的离线回放测试；
   - `nginx`（60 条）：校验 Nginx 入向/出向/TLS 配置与路由的 7 个集成/部署测试模块；
   - `deploy_tooling`（493 条）：`tests/tooling/test_m2_*.py`、`tests/tooling/test_m3_*.py`、Gateway 安装及环境连续性等 27 个运维脚本与里程碑工具测试模块；
   - `slow`：单用例墙钟耗时 ≥ 3 s 预留标记。
2. **本地默认屏蔽 `docker`/`browser`，定向运行自动放行**：
   - 在 `tests/conftest.py` 新增 `_should_apply_local_default_markers(config)`，仅当 **非 CI（`CI` 未设）、未设置 `AIOPS_PYTEST_ALL=1`、调用方未传 `-m`/`-k`、且未指定具体测试文件/子目录（仅裸跑 `uv run pytest` 或 `pytest tests`）** 时，默认注入 `markexpr = "not docker and not browser"`。
   - 开发者直接运行如 `uv run pytest tests/dify/test_dify_idempotent_import.py` 或 `-m docker` 时**零过滤**直接执行目标重型用例。
3. **运维快捷脚本 `tools/test.sh` 增加 `--fast` 与 `--all`**：
   - `./tools/test.sh --fast`：额外屏蔽 `deploy_tooling` 与 `nginx`（仅跑核心业务/架构守卫 `5,416/5,976` 条）；
   - `./tools/test.sh --all`：设置 `AIOPS_PYTEST_ALL=1`，强制包含全部 `docker` 与 `browser` 在内的 `5,976` 条全量用例。
4. **CI 路径感知重型档位路由（`.github/workflows/ci.yml`）**：
   - `Decide the test tier` 保留原有 `tier=docs|full` 白名单合同，并新增输出 `run_docker` 与 `run_browser`：
     - 仅当非 PR（缺 base/head）、空 diff、改动触及 Dify 部署/运行期路径（`deploy/dify*`、`ops/dify*`、`src/aiops_diagnostics/dify/`、`tests/dify/`）或共享测试/CI 配置时，`run_docker=true`；
     - 仅当改动触及消费者侧路径（`src/aiops_diagnostics/consumer/`、`tools/verification/consumer-status/`、`tests/consumer/`、`static/`）或共享测试/CI 配置时，`run_browser=true`；
     - 其他 `full` 档 PR 自动注入 `-m "not docker and not browser"` 跳过这 7 条重型用例，同时 `floor=4000` 与 `skipped <= 10` 门禁继续全额生效。
   - `Run tests` 显式传入 `MALLOC_ARENA_MAX=2` 与 `-n 4`，并扩展 `tests/test_ci_test_tier.py` 与 `tests/test_parallel_setup.py` 钉死路由与本地过滤回归边界。

## Alternatives considered

### 1. 在 `tests/dify` 与 `tests/consumer` 使用 `@pytest.mark.skipif(...)` 跳过重型用例
- **最强论点**：无需改写 `tests/conftest.py` 参数钩子或 CI workflow。
- **否决原因**：`skipif` 会在 JUnit XML 中记为 `skipped`，而 `tools/verification/ci_results.py` 强制 `skipped <= 10` 作为防静默退化防线；通过 `-m` 收集期过滤（deselect）可在保证 `skipped` 不膨胀的同时精确按改动域启用重型检查。

### 2. CI 默认在所有 PR 上屏蔽 `deploy_tooling` 与 `nginx`（仅 `--fast` 子集上 CI）
- **最强论点**：可进一步将 CI 常规测试量从 ~5,969 条再削减 ~553 条。
- **否决原因**：`nginx` 仅 60 条且直接守护生产反代与 TLS 配置边界，`deploy_tooling` 多为毫秒级纯脚本单测；将它们保留在 CI 与本地默认档、`--fast` 显式按需关闭，能在 CI 安全性与开发者日常迭代效率间取得稳妥平衡。彻底清理历史 M2/M3 工具测试将作为下一阶段独立退役清单（P2）经确认后执行。

## Consequences

在 8 核 / 15 GiB 宿主机上（配合 `umask 022` 下全量基准与 ADR-0354 优化），PR B 实测各档位表现如下：

| 运行方式 | 执行用例范围 | 墙钟耗时 | 进程树总峰值 RSS | 单进程峰值 RSS |
|---|---|---:|---:|---:|
| 本地默认 `uv run pytest`（`-n auto`=2） | `5960 passed, 9 skipped`（屏蔽 7 条 `docker`/`browser`） | 582.8 s | 1,207.7 MB | 372.3 MB |
| 极速反馈 `./tools/test.sh --fast`（`-n auto`=2） | `5407 passed, 9 skipped`（再屏蔽 `nginx`/`deploy_tooling` 553 条） | 524.5 s | **1,146.1 MB** | 350.3 MB |
| CI 对齐 `./tools/test.sh --all -n 4` | `5967 passed, 9 skipped`（全量 5,976 条无过滤） | **309.0 s** | **1,542.1 MB** | **317.6 MB** |

- 对于普通 PR（不触及 Dify 部署与消费者静态/回放代码），CI 无需拉起 Docker 容器与 Chromium 即可完成核心回归验证；触及对应域或共享配置时自动恢复完整覆盖。
