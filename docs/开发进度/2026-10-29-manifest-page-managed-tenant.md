# 环境清单交还页面管理的管家

分支`fix/env-manifest-page-managed-tenant`，基线`436b4f7b`。

## 目标

让`ops/environments/env-41.toml`与线上"谁在管这个入口"一致：优链充(1941847786056323072)的
operator入口已由Dify业务页面开通并发布，清单不应再点名它的app。

## 已完成

- 删掉该租户的`[[agents]] ops-youlian-order-diagnosis`与`[[dify_apps]] app_id=349a637c…`，
  原地留下为什么删、删了会怎样、以后新租户怎么做的注释。
- 补一条此前**没有测试**的回归：页面管理的`(租户, operator)`让整份清单在写入前中止，
  且`--prune`不删页面管理的行。

## 验证

- 本地用真实仓库清单+复刻41注册表状态：改前`REFUSED: 页面管理的管家不能由环境清单覆盖`，
  改后两条现存绑定均`unchanged`。
- 变异检验：把守卫改成`pass`后新测试必红（`DID NOT RAISE`），用内存原文写回后复绿。
- 相关套件62 passed；`ruff check`/`format --check`、`policy-check`通过。

## 已知缺口

41上对该清单的`aiops-admin reconcile`仍会在写入前中止，但原因是**另一个既有阻塞**：
`_preflight`的模型白名单（`production.env`是`deepseek-v4-1-flash-261017`，4个现役agent是
`deepseek-v4-flash`）。用改动前的清单跑同一dry-run输出逐字相同，证明与本次改动无关。
该阻塞需单独决策（改白名单或升级4个agent），本轮不动；因此守卫的修复**未在41端到端验证**。

## 下一步

- 决策模型白名单漂移；解决后按`env-41-runbook.md` §3.1把仓库清单装成`env-41.toml.new`再实跑，
  验收标准是连续两次全`unchanged`。
- 新租户流程加一步：经页面开通后从清单删掉它的`[[dify_apps]]`。
