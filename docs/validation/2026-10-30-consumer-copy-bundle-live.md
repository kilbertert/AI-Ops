# 文案包上线与 CD 排队的处置记录

日期：2026-10-30（UTC）。环境：41（生产）+ GitHub Actions。方式：只读核对 + 一次取消卡住的
workflow run（不改任何服务配置、不改业务数据）。

被验对象：#1211（`ae290a0d`），决策见 [ADR-0186](../adr/0186-consumer-copy-is-a-published-bundle.md)。

## 部署事实

| 项 | 证据 |
| --- | --- |
| 合并 | #1211 → `ae290a0d` |
| CD | run `41028624018` success |
| 线上修订 | `/health` → `1.0.0+ae290a0d6bf1`，`business_mutations=disabled` |
| 新代码在位 | `/opt/aiops-41/src/aiops_diagnostics/consumer/consumer_copy.py`（mtime `2026-10-30T16:45:37+08:00`） |

## 事故：CD 排队 12 分钟，不是 CD 坏了

| 时间（UTC） | 事实 |
| --- | --- |
| 08:45:40 | CD `41028624018` 进入 `queued` |
| ~08:57 | 查 `jobs` → `deploy-41 status=queued runner=`（**未分配**）；查 runner → `online busy=true`；查 `in_progress` → 只有 `Agent Update Branch` `41025764974`，`started_at=08:21:11`，已 39 分钟 |
| 08:58 | `gh run cancel 41025764974`（该 run 的 PR `feat/consumer-copy-bundle` 已于 08:4x 合并，job 无事可做） |
| ~08:59 | runner 释放，CD job 拿到 runner 并启动 |
| ~09:12 | CD `completed/success` |

根因：唯一的 self-hosted runner 被一个卡住的 `Agent Update Branch` job 占着；
`agent-update-branch.yml` 的 `timeout-minutes: 60`，所以它最长能占一小时。CD 侧的
`concurrency: cd-41-deploy` 不参与这件事 —— job 连机器都没拿到。

**没有做**：没有改 `agent-update-branch.yml`（它属于 `policy-check.mjs` 的策略管理 workflow，
改它是策略变更），也没有放宽任何检查。

排查步骤已写进 [41 手册 §5.4](../agents/env-41-runbook.md)。

## 未完成项（不得读作通过）

- **文案包没有在真实租户上发布过**：本轮只确认了代码与 CLI 在线上，没有产生任何版本。
  真机观感同样未验。
- 只有 `zh`／`en` 可改；其余九种仍是整段模板（`validate` 明确拒绝）。
- 节点路线（`answer_from_run`）仍用内置文案，本片未接。
