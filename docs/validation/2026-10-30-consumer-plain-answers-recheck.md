# C 端答复改版：CD 推进后的复验

日期：2026-10-30（UTC）。环境：41（生产）。方式：**只读**——取真实会话、读业务库、
向公网入口发一次状态问法。未改配置、未写业务数据。

被验对象：#1175（`5de086c3`）+ #1202（`0b58d502`）。
前一轮的验收见
[2026-10-30-consumer-plain-answers-live-verified.md](2026-10-30-consumer-plain-answers-live-verified.md)，
决策见 [ADR-0182](../adr/0182-consumer-answers-lead-with-the-conclusion.md)，
本轮里程碑见[开发进度](../开发进度/2026-10-30-consumer-plain-answers-recheck.md)。

## 为什么还要复验一次

前一轮核对时线上是 `1.0.0+0b58d502adc5`。之后 CD 又部署了 #1205，
`/health` 变成 **`1.0.0+be78e7c21e10`**。两条记录都会被人拿来和 `/health` 比对，
所以需要说清"验的是哪个修订、之后有没有变、变了之后还成立吗"。

## 事实

| 项 | 值 |
| --- | --- |
| 当前线上修订 | `1.0.0+be78e7c21e10`（`business_mutations=disabled`） |
| 本片代码是否在线上 | 在：`/opt/aiops-41/src/aiops_diagnostics/consumer/consumer_plain.py`（mtime `2026-10-30 13:48:12 +0800`） |
| #1205 是否影响这条渲染路 | 不影响：它只动管家的工具声明（`operator_tools` / Java `GovernedToolRuntime` 等），`consumer_plain` 未被触碰 |

## 复跑结果

同一租户 `…861824`、同一订单 `2107546575906942977`，公网入口
`POST /v1/assistant/questions` → `status=completed`：

```
这单当前的状态是「充电结束」。

订单号：2107546575906942977
查询于：2026-10-30 07:12:34 UTC

这只是我查到的业务状态，不代表故障原因。
来源：充电状态记录
```

结论：新格式在**当前**线上修订上仍然成立；没有 `cpms.*`、没有 `status_code=`、没有微秒 ISO。

## 仍然未验

- 真机／原 H5 上的**观感**（结论句是否在第一屏、`·` 与编号的对齐）——只能人眼看。
- 九种非中文译文**未经母语者复核**。
- 本轮只复跑状态问法；账单问法在前一轮已验过，本轮未重复。

操作知识自检：方法与 [41 手册](../agents/env-41-runbook.md) §4／§5 一致，
未引入新步骤；未把会话令牌或口令写进仓库。
