# ADR-0357: P2 依赖核查收口：`deploy_tooling` CI 路径门禁、`nginx` 标记补全与热点用例收敛

Status: Accepted
Date: 2026-11-26

## Context

在依据 [ADR-0356](https://github.com/kilbertert/AI-Ops-history-archive/blob/4bed22cbabbcbe80f242c83275a46d3f550f996d/docs/adr/0356-retire-historical-recon-evidence-tests.md) 退役 6 个历史 `#475–#480` 公司探查文档测试后，对其余 P2 候选测试项（M2/M3 工具测试、Nginx 配置测试与超大 `@pytest.mark.parametrize` 矩阵）进行了跨 `.github/workflows/ci.yml`、`tools/README.md`、`src/` 与 `docs/agents/` 的全仓依赖核查：

1. **M2/M3 工具资产仍处于现役调用链路，不得按里程碑名称整批删除**：
   - `.github/workflows/ci.yml` 的 `Build and inspect Gateway image` 步骤直接调用 `deploy/m2-live-03/build-image.sh` 与 `images.lock.json`；
   - `deploy/m2-live-03/`（22 文件）、`services/model-proxy/`、`tools/verification/m2_live/`（5 文件）与 `tools/experiments/m2_close/`（含现役文档与 QA 引用）均有活跃维护者；`tools/README.md` 明确提示“不能按‘工具/旧里程碑’整体删除”；
   - 原 `deploy_tooling` 标记集里的 3 个文件（`test_m2_live_isolation.py`、`test_m3_consumer_snapshot.py`、`test_m3_node_link_completion.py`）直接测试 `src/aiops_diagnostics/` 核心隔离/执行逻辑，不应被 `--fast` 或 CI 工具路由跳过；而另有 4 个真实启动 Nginx 的测试模块漏打了 `nginx` 标记。
2. **高成本热点用例与参数矩阵可无损提速/收敛**：
   - `tests/tooling/test_m2_live03_model_proxy.py` 在 21 条用例中每次生成一次 RSA-2048 自签证书并固定 `time.sleep(0.5)` 等待回环端口（总耗时 **11.91 s**，其中至少 10.5 s 为纯等待 + 21 次密钥生成）；
   - `tests/gateway/test_setting_sources.py` 的 3 项全配置表校验通过 `@pytest.mark.parametrize` 展开为 234 个独立 pytest item（全文件共 250 item），放大收集与 fixture 调度成本；而 `test_dify_console_publish.py`（3.27 s）、`test_company_token_auth.py`（2.40 s）与 `test_dify_login_proxy.py`（3.72 s）属鉴权与安全边界核心断言且本身仅 2–3 s，应全额保留。

## Decision

落实 P2 第二批结构收敛与性能收口：

1. **修正 `deploy_tooling` 边界并补齐 `nginx` 标记**：
   - 从 `test_m2_live_isolation.py`、`test_m3_consumer_snapshot.py`、`test_m3_node_link_completion.py` 移除 `deploy_tooling` 标记，使 `src/` 核心隔离与任务状态测试常驻开启；保留其余 24 个运维/安装/里程碑脚本测试文件（合计 **423 条**）的 `deploy_tooling` 标记。
   - 为 `tests/consumer/test_consumer_runtime_delivery.py`、`tests/operations/test_environment_user_ingress.py`、`tests/operations/test_operator_h5_deploy.py`、`tests/operations/test_operator_token_ingress.py` 补齐 `pytestmark = pytest.mark.nginx`（全部 `nginx` 标记共 **118 条**）。
2. **为 `deploy_tooling` 接入 CI 路径感知门禁（`.github/workflows/ci.yml`）**：
   - `Decide the test tier` 同步输出 `run_deploy_tooling`：仅当改动触及 `deploy/`、`ops/`、`services/`、`tools/`、`tests/tooling/`、CI/配置底座，或非 PR / 空 diff 时开启；普通业务/网关 PR 在不损核心覆盖的前提下自动加入 `not deploy_tooling`（同步扩展 `tests/test_ci_test_tier.py` 回归矩阵）。
3. **无覆盖损失优化热点文件**：
   - `tests/tooling/test_m2_live03_model_proxy.py`：复用模块级 `_shared_tls_certs()`（`@functools.lru_cache(maxsize=1)` + `atexit` 清理临时目录），并将 `_start()` 的硬编码 `time.sleep(0.5)` 改为 `_wait_port()` TCP 就绪轮询。
   - `tests/gateway/test_setting_sources.py`：将 3 条全表参数化用例折叠为 3 条带 `monkeypatch.context()` 的全字段循环断言（覆盖全部 78 个字段不变，pytest item 由 250 降至 22），并将 `test_every_setting_source_has_a_runtime_consumer` 迁移至 `common.source_ast_cache`。
4. **同步文档**：在 [repository-layout.md](../architecture/repository-layout.md) 与 [tools/README.md](../../tools/README.md) 记录本次 `deploy_tooling` 路径门禁及现役资产核查结论。

## Alternatives considered

### 1. 不区分引用现状直接删除 `test_m2_*.py` 与 `test_m3_*.py`
- **最强论点**：可再删去 23 个文件 / 351 条用例。
- **否决原因**：`deploy/m2-live-03/`、`services/model-proxy/`、`tools/verification/m2_live/` 与 `src/` 隔离守卫均为现役资产；失去对应回归测试会让 Gateway 镜像构建与受控模型代理缺乏校验。按路径门禁触发可在保护现役资产的同时消除日常业务 PR 的无效等待。

### 2. 对 `test_dify_console_publish.py`、`test_company_token_auth.py`、`test_dify_login_proxy.py` 做参数裁剪
- **最强论点**：可再减少约 300+ pytest 计数。
- **否决原因**：实测三者各自仅耗时 2.4–3.7 s，且每条参数覆盖不同的伪造 Origin / 角色越权 / 开放重定向 / CSRF 拒绝路径；削减安全边界矩阵收益微弱却直接引入安全回退风险。

## Consequences

- `tests/tooling/test_m2_live03_model_proxy.py`（21 条）：由 **11.91 s** 降至 **1.55 s**（约 **7.7 倍** 提速）；
- `tests/gateway/test_setting_sources.py`：由 **250** item 降至 **22** item（1.24 s，保留全部 78 字段等价断言）；
- 全量测试集最终收敛规模 **5,693 条**（对比初始 **5,970 条**），在 `umask 022` 宿主机实测结果如下：

| 档位 | 执行结果 | 墙钟耗时 | 进程树总峰值 RSS | 单进程峰值 RSS |
|---|---|---:|---:|---:|
| `./tools/test.sh --fast`（`-n auto`=2） | `5144 passed, 1 skipped` | 542.8 s | **1,092.6 MB** | 382.4 MB |
| `./tools/test.sh --all -n 4`（全量无过滤） | `5692 passed, 1 skipped` | **302.8 s** | **1,565.5 MB** | **329.5 MB** |
