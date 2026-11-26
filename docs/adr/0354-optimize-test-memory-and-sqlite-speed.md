# ADR-0354: 控制测试工作进程内存膨胀、修复跨用户槽锁并加速测试期 SQLite 与源码守卫

Status: Accepted
Date: 2026-11-26

## Context

随着项目持续演进，测试集已增长至 336 个文件、约 5,970 条用例（11.4 万行测试代码）。在 8 核 / 15 GiB 开发宿主机上执行基线全量测试（`uv run pytest -n 4 -q`）存在以下可测瓶颈：

1. **多项目并发测试触发宿主机 OOM 风险**：
   - 4 并发基线全量跑总峰值 RSS 达 **2,406.3 MB**，单 worker 峰值 RSS 从启动时的 26.8 MB 持续攀升至 **501.2 MB**；
   - 通过 `tracemalloc` 与 `gc.get_referrers()` 实测归因：一方面 glibc 默认按核数分配多个 malloc arena（同工作负载对比：`MALLOC_ARENA_MAX=2` 可把 RSS 从 198.0 MB 降至 124.6 MB）；另一方面，`fastapi.dependencies.models` 的三个内部 LRU 缓存（`_is_gen_callable_cached`、`_is_async_gen_callable_cached`、`_is_coroutine_callable_cached`，`maxsize=4096`）以 `_CallIdentity(endpoint_func)` 强引用了每轮 `create_gateway_app()` 生成的路由闭包，导致单个 worker 在运行数十个网关测试后同时滞留 **34 个完整 `FastAPI` 应用图与 19,618 个 Pydantic `TypeAdapter`/`SchemaValidator` Rust 对象**；此外全仓 34 个文件存在 37 处未显式关闭的 `TestClient` / `GatewayRuntime`。
   - `tests/conftest.py` 中的宿主机全局并发槽锁 `_acquire_host_slot` 使用原生 `open(..., "w")` 而非带 `0o666` 权限的 `_open_slot`，在 `umask 027` 下生成 `0640` 锁文件，使跨用户/容器 UID 的并发上限护栏失效；且锁句柄依赖进程退出回收而未在 `pytest_unconfigure` 显式释放。
2. **零覆盖损失的热点慢点**：
   - `tests/test_readme_architecture.py::test_readme_module_references_point_at_real_files` 使用未剪枝的 `PROJECT_ROOT.rglob("*.py")` 扫描整棵 `.venv/` 目录，单条用例耗时 **17.68 s**；
   - `src/aiops_diagnostics/gateway/gateway_db.py` 在全量测试中对 `/tmp` 临时 SQLite 数据库默认执行 `PRAGMA synchronous = FULL`，单库高频小事务实测较 `OFF` 慢 **7.5 倍**（60 次事务 182.1 ms vs 24.3 ms）；
   - 10 余个静态架构守卫测试各自重复遍历 `src/` 读取源码与解析 AST。

## Decision

在**保持零测试覆盖率损失**的前提下，落地以下低成本高收益改进（P0）：

1. **抑制 worker 内存常驻并感知宿主机可用内存**：
   - 在 `tests/conftest.py`（于 xdist worker `execve` 派生前）与 `tools/test.sh` 默认设置 `MALLOC_ARENA_MAX=2`。
   - 在 `pyproject.toml` 将默认参数改为 `-q -n auto`，并在 `tests/conftest.py` 实现 `pytest_xdist_auto_num_workers`：CI 环境固定 4 并发；本地默认依据 `/proc/meminfo` `MemAvailable`（`>= 4096 MiB` 时为 2，否则退化为 1），并支持 `AIOPS_PYTEST_WORKERS` 显式覆盖。
   - 在控制器 `pytest_configure` 并发运行前通过 `_acquire_memory_budget` 检查 `MemAvailable`（默认 `DEFAULT_MIN_FREE_MB=1536` 或由 `AIOPS_PYTEST_MIN_FREE_MB` 覆盖，超时快速失败以免压垮宿主机）；修复 `_acquire_host_slot` 改用 `_open_slot` 确保 `0o666` 权限，并在 `pytest_unconfigure` 显式关闭锁句柄。
2. **自动回收 FastAPI 缓存、`TestClient` / `GatewayRuntime` 与堆碎片**：
   - 新增 `_clear_fastapi_callable_caches(threshold=256)`：在 `pytest_runtest_teardown` 检测 `fastapi.dependencies.models` 三个可调用判定 LRU 缓存大小达到阈值时清空，并联动 `gc.collect()` 与 `libc.malloc_trim(0)`（否则每 50 条用例定期清理一次）。隔离实测可立即把存活 `FastAPI` 实例从 34 降至 0、`TypeAdapter` 从 19,618 降至 0。
   - 通过 `weakref` 跟踪测试内创建的 `TestClient` 与 `GatewayRuntime`，在对应 fixture/call 生命周期结束时按创建逆序自动调用 `close()` / `shutdown()`。
3. **仅限 `/tmp` 测试目录的 SQLite `PRAGMA synchronous = OFF`**：
   - 在 `gateway_db.py` 增加模块级测试开关 `_TEST_SYNC_OFF_ROOT: Path | None = None`；`tests/conftest.py` 仅在 `AIOPS_TEST_REAL_FSYNC != "1"` 时将其设为解析后的临时目录根（`tempfile.gettempdir()`）。仅当目标库文件位于该临时根目录下且非只读连接时使用 `PRAGMA synchronous = OFF`，生产路径与真实存储语义零变化。
4. **修复未剪枝全仓扫描并提取公共只读文件/源码助手**：
   - 修复 `tests/test_readme_architecture.py`，改为剪枝 `.venv`/`.git`/缓存目录的 `os.walk` 遍历。
   - 新增 `tests/common/source_ast_cache.py` 提供 `iter_python_files`、`read_source_text`（带 `(path, mtime_ns, size)` 指纹的有界 LRU 缓存，上限 256）与按需 `parse_source_ast`，将 10 个全量 `src/` / `tests/` AST 与源码守卫测试统一迁移至该助手。

## Alternatives considered

### 1. 长期 `@lru_cache` 缓存全部已解析的 `ast.Module` 语法树
- **最强论点**：跨多个 AST 守卫测试完全消除重复 `ast.parse()` CPU 开销（约 0.5–1.2 s）。
- **否决原因**：实际采样验证发现，将 242 个 `src/` 与 354 个 `tests/` 模块的 `ast.Module` 常驻进 `@lru_cache(maxsize=512)` 后，单个 worker 进程仅 AST 对象就长期占用 **259 MB** RSS（总进程 RSS 从 501 MB 反向膨胀至 792–850 MB）。相比 1 秒级 CPU 耗时，内存代价比完全失衡，因此只缓存有界源码文本、AST 坚持即时解析并随循环释放。

### 2. 为 `pytest` 启用 `--forked`（pytest-forked）为每个用例或每 N 个用例强制派生子进程
- **最强论点**：OS 进程退出即可绝对清空所有 Python 缓存与原生堆碎片，无需针对特定框架写清理钩子。
- **否决原因**：本测试集接近 6,000 条用例，若高频 fork 子进程会显著抬高收集与启停开销，且与部分带线程后台等待的用例及现有并行架构兼容性差；相比之下，清理 FastAPI 缓存 + 限制 `MALLOC_ARENA_MAX` + `malloc_trim(0)` 以近零时间成本消除了核心内存增长。

### 3. 直接在 `pyproject.toml` 硬编码固定 worker 数且忽略宿主机剩余内存
- **最强论点**：配置最简单，所有环境行为一致。
- **否决原因**：无法阻止开发者在本机多项目同时运行测试时触发内存耗尽；通过 `pytest_xdist_auto_num_workers` 区分 CI 与本地可用内存动态档位，并保留 `AIOPS_PYTEST_WORKERS` 覆盖口，可兼顾单仓速度与多仓并发安全。

## Consequences

在相同 8 核 / 15 GiB 宿主机、相同测试覆盖（`5965 passed, 9 skipped`，0 失败）下，PR A 实测前后对比如下：

| 运行档位 | 墙钟耗时 | 进程树总峰值 RSS | 单进程峰值 RSS | 变化摘要 |
|---|---:|---:|---:|---|
| 基线 `pytest -n 4`（优化前） | 326.7 s | 2,406.3 MB | 501.2 MB | 基线（4 workers） |
| PR A `pytest -n 4`（CI 对齐档） | **313.4 s** | **1,586.0 MB** | **317.0 MB** | 总 RSS **-34.1%**（-820.3 MB），单进程 **-36.8%**，耗时 -13.3 s |
| PR A `pytest`（本地默认 `-n auto`=2） | 625.8 s | **1,205.7 MB** | **370.5 MB** | 总 RSS **-49.9%**，单进程 **-26.1%**，适合多项目并行 |

- 需要极速本地开发循环的命令级分层筛选（`docker` / `browser` / `nginx` / `deploy_tooling` pytest marker 及 CI 路径按需触发）作为下一阶段 PR B（ADR-0355）独立交付，本变更保持全量测试 100% 运行、不更改任何验收门禁数量。
