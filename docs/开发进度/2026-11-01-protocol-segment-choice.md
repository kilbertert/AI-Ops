# 协议块补"只引用本轮实际给出的段"（2026-11-01）

## 目标

把订单查询的失败率从"约 1/3"压下去。失败形态已经换过一次，这次是**合法 JSON、
错内容**。

## 已完成

- 提交 `cc4e7ab9`（分支 `fix/protocol-segment-choice`，PR #1298）：
  `PROTOCOL_BLOCK` 补一句"只引用本轮实际给出的段：看不到 billing_line 就不要写它，
  看不到 status_line 也不要写它"，并新增一条测试钉住这句话必须在协议块里。
- 全量 `pytest -q` 退出码 0；`ruff check` 通过；`context_digest.py --check` `match: true`。

## 根因

`#1294` 部署（`1.0.0+56995d3d1153`）后连续 9 次真实订单查询：**6 成功 / 3 失败**。
失败三条全部是这个形状：

```
{"kind":"realtime","answer":"","reference_ids":[],"reply":"{{fact:status_line}}\n{{fact:billing_line}}" }
```

本次是**状态查询**，快照里只有 `status_line` / `observed_line`。模型照
"账单必须引用billing_line"那句写上了不存在的段，`render_segments` 按设计拒绝
（`CHATFLOW_FACT_UNKNOWN`）。守卫没错，缺的是让模型不必猜的说明。

## 关键缺口（Devin 在 #1298 指出，尚未完成）

**协议块是 Dify 工作流里保存的 SYSTEM 文本，模型运行时不读 Python 模块。**
本 PR 只改了模板与测试；生产仍在用旧协议，所以这一轮改动**单独合并不会改变生产行为**。
需要：用 `tools/content/upgrade_consumer_flow.py --live-documents` 迁移现役工作流的
协议块 → 经 `/v1/dify/publish` 发布 → 再重跑订单查询。

## 验证边界

- 新增测试只证明"协议块里有这句话"，**不证明**模型不再引用缺失的段 ——
  那只能靠部署后的真实订单复测。
- 尚未部署、尚未迁移。

## 下一步

1. 合并 PR → CD 部署网关（只带来代码，不改变已保存的 Dify 协议）。
2. 迁移 Dify 工作流协议块并发布（`--live-documents`，然后 `/v1/dify/publish`）。
3. 同一订单连续 ≥10 次真实会话，要求 `CHATFLOW_FACT_UNKNOWN` 消失。

**业务验收未完成**：41 原应用 / 原 H5 的正式验收仍未全部通过。
