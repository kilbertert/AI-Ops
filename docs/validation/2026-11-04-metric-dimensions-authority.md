# 度量维度词表收口：trial 止血与三维守卫

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

**新增 25 条测试**（`tests/gateway/test_metrics_dimensions.py`）：

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

全量 **4128 passed / 9 skipped**（含 `tests/gateway/test_metrics_dimensions.py` 25 条）。镜像内容锁真实重建，`context_digest.py --check` 一致。

## 三之二、复审订正（同一 PR 内）

三条全部成立，逐条修：

1. **空字符串回归（真回归）**：`check_dimension` 第一版写 `if allow_empty and not value`，
   于是 `?route_type=` 从 422 变成「返回空列表」。`None`（不过滤）与 `""`（非法过滤值）
   是两件事，改成 `value is None`。补 `test_an_empty_route_filter_is_still_rejected`。
2. **常量映射让守卫失效**：测试里硬编码了 `ROUTE_TRIAL: "trial"`，常量被改成词表外的值
   时守卫照样通过。改成从模块 `getattr` 取真实值。
3. **试运行测试只测存储层**：直接调 `store.record()` 测的是"存储层收不收"，
   而真正会坏的是"生产点还在不在写"。改成构造最小 runtime 替身、调用
   `_record_trial_metric` 本身。

**变异验证（四条全中）**：

| 变异 | 结果 |
|---|---|
| 从词表删掉 `trial` | 2 条红 |
| 判据写回 `list_runs()` 第二处 | 1 条红 |
| 生产点改成 `route_type="trial_typo"` | 2 条红 |
| 常量值改成 `trial_typo` | 2 条红 |
| `allow_empty and not value`（退回空串放行） | 1 条红 |

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
