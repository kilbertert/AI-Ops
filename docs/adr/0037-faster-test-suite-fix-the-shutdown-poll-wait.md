# 37. 测试套件提速：先修那个 0.5 秒的等待（并行留到下一轮）

日期：2026-10-18
状态：部分采纳 —— 等待已修；**并行被撤回**，理由见"被否掉的替代形态"
影响版本：`tests/conftest.py`（新增）；不改任何运行时代码、不改任何测试断言

## 背景

全量 `uv run pytest` **299.62 s**，迭代时不可接受。逐文件计时（148 个文件）显示
耗时极度集中：

| | 占全量 |
|---|---|
| 最慢 1 个文件 | **35%** |
| 前 5 个 | **71%** |
| 前 10 个 | **94%** |

所以问题不是「测试多」，是**少数文件**和**一个全局等待**。

`--durations` 指向了原因：几乎每个用例的 `teardown` 都是**整整 1.00s**，而用例本身
只要几十毫秒到 1s。

## 诊断

`tests/test_dify_console_publish.py` 每个 fixture 这样收尾：

```python
threading.Thread(target=server.serve_forever, daemon=True).start()
yield server.server_address[:2]
server.shutdown()
server.server_close()
```

`socketserver.BaseServer.serve_forever(poll_interval=0.5)` 的默认值是 **0.5 秒**，
源码注释写着 `"Polls for shutdown every poll_interval seconds"`，而 `shutdown()`
**阻塞到循环退出**。近百个用例、每个开两个服务器 ⇒ 每个用例固定等约 1s。

本仓 `src/` 里的四个 `serve_forever()` 都在 `main()` 里，是**生产服务器**，
不能为测试去改它们的等待粒度。

## 决策

**`tests/conftest.py` 把库默认值收紧到 0.01s，只在调用方省略参数时。**

测同一文件：

| | 耗时 |
|---|---|
| 默认 0.5s | **96.73 s** |
| `poll_interval=0.01` | **2.97 s** |
| 全量（修前串行） | 299.62 s |
| 全量（修后串行） | **218.65 s** |

只改**等待粒度**，不改服务器行为。不用 0 是因为 `selector.select(0)` 会变成忙等烧 CPU；
0.01 比默认快 60 倍且开销可忽略。

**调用方显式传参时不得被覆盖。** `src/aiops_diagnostics/tdengine_proxy.py` 明确传 0.5。
第一版用 `poll_interval: float = 0.5` 作形参默认值，于是**无法区分「省略」与「显式传 0.5」**，
把显式调用也改成了 0.01——review 抓出。改为私有哨兵 `_UNSET`：省略时传 0.01，
显式时原样转发。三种情况的转发值实测为 `[0.01, 0.5, 0.25]`。

## 被否掉的替代形态

- **`addopts = "-q -n 4"`（并行）。本 PR 曾包含，已撤回。** 撤回不是因为慢，是因为
  **它会让这个测试套件间歇性变红**：
  - `tests/test_merge_faq_i18n_tool.py` 的 8 个测试直接改写**真实仓库资产**
    （`faq_catalog.json`、`faq_recommendations.json`、`operator_answers_vi.json`）
    再在 `finally` 里恢复；
  - 而 `FAQCatalog.bundled()` 在 **24 个其他测试文件**里对同一个 `faq_catalog.json`
    做裸 `read_text`；
  - 实测该文件：`-n 4` → **4 failed / 5 passed**；`-n 0` → **9 passed**。
    决定性对照，可复现。
  - 并发窗口实测存在：3 个读者 + 30 次写循环 ⇒ **411 次坏读取**，含
    `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`。
  - 曾有一次全量 `-n 4` 连跑 3 次全绿（2738 passed），**那是侥幸**——窗口是几十毫秒，
    不是稳定通过。用「跑三次都绿」当作并行的验收判据是不够的；必须**逐文件**验证
    或者先消除共享状态。

  这一条值得记住，因为它的失败形态很坏：**看起来像代码问题，实际是测试间的竞争**。

- **只开并行、不修等待。** 见上：并行会立刻暴露共享状态问题，而等待是完全独立的、
  零风险的收益（27%）。

- **把 `poll_interval` 写进每个测试的 `serve_forever()` 调用。** 14 个调用点，
  每处一行，但漏一处就漏一处，且要碰 5 个测试文件。`conftest.py` 一处覆盖全部。

- **改 `src/` 里的 `serve_forever()`。** 那是生产服务器的 `main()`，为测试去动它，
  等于为了测试改变生产行为。

## 后果

- 全量 **299.62 s → 218.65 s（−27%）**，零并发风险，零测试断言改动。
- CI 的 `uv run pytest -q` 无需改动。
- **并行的收益（→76.5 s）仍在桌上，但被明确推迟。** 前置条件：把那 8 个改写真实资产的
  测试改成在私有工作树上跑（工具的所有路径都由 `ROOT = Path(__file__).parents[1]` 派生，
  且不 import 项目包，所以复制到临时根目录是可行的）。
- **下一轮的验收判据不能是「全量跑三次都绿」**——本次教训是那不够。应当是：
  ① 逐文件对比串行/并行结果；② 或者先证明没有共享可写状态。
