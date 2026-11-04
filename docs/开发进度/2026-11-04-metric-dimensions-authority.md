# 里程碑：度量维度词表收口（#799 第一步）

## 目标

41 上试运行发生了 181 次、`agent_run_metrics` 里 trial 零行 —— 指标被静默丢弃。
本轮止血，并建立让「下一次词表少成员」在 CI 红的守卫。**未部署**。

日期：2026-11-04。分支：`fix/metrics-dimension-authority`；基线 `9ca54968`；ADR-0206。

## 一、现网损失（41 只读实测）

```text
agent_run_metrics 按 route_type：
  clarification 240 / routing 194 / qa 162 / diagnosis 96 / promo 76
  operations_analysis 36 / faq 25
  trial —— 零行

agent_runs 里 operator-trial 绑定的运行数：181
```

**181 次试运行，0 行指标。** 每次 `POST /v1/dify/trial` 跑完，`record()` 都被
`ROUTE_TYPES` 拒绝，异常被 `contextlib.suppress` 吞掉，请求本身依然成功 —— 所以
从用户侧完全看不出来。

## 二、改了什么

1. `ROUTE_TYPES` 补 `trial`（止血），并为每个成员加「谁写它」的注释。
2. `check_dimension()` 收成唯一判据，`record()` 与 `list_runs()` 共用。
3. 生产点 `_record_trial_metric` 改引用 `ROUTE_TRIAL` 常量。

## 三、验证

**新增 24 条测试**（`tests/gateway/test_metrics_dimensions.py`）：

- 三个维度 × 每个成员 = 18 条「真实 record + list_runs 有行」
- 判据不重复（源码扫描）
- 产而无判据（生产点字面量 vs 词表对账）
- `trial` 正例
- 未知值仍被拒（对照组）

**变异验证（三条全中）**：

| 变异 | 结果 |
|---|---|
| 从词表删掉 `trial` | 2 条红（正例 + 产而无判据） |
| 把判据写回 `list_runs()` 第二处 | 1 条红（判据不重复） |

全量 **4126 passed / 9 skipped**。镜像内容锁真实重建，`context_digest.py --check` 一致。

## 四、过程中的两处自我纠正

1. **「判据而无生产者」的机械断言写不出来。** 第一版写了，实测 10 个成员全部误报 ——
   `faq`/`debug`/`routing` 经由 `route_tag` 变量产生，按字面量 grep 读不到。
   删掉这条，并在测试 docstring 里写明为什么不做：硬写会红，然后被 `xfail` 关掉，
   守卫等于没有。
2. **import 放错作用域**：`metrics_dims` 被塞进 `__init__` 内部，第 951 行在另一个
   函数里用不到。`ruff` 的 `F821 Undefined name` 当场抓到。

## 五、边界

- **未部署**。生产仍是改前版本；部署后需复测 `/v1/agent-metrics/runs?route_type=trial`
  从「恒 0 行」变为可返回行。
- 其余生产点仍是裸字面量（`faq`/`qa`/`diagnosis` 等）。它们不在词表里缺成员，
  没有现网损失，本轮不扩大改动。
- **`retrieval_status` 的托管值仍未显式化**——#799 验收第 5 条留给后续。这一条同样是
  「下一次静默丢失」的候选路径，本轮不碰也不假装修了。
