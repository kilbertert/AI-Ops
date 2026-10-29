# 清单交还页面管理：本地验证与41只读核对

日期：2026-10-29。分支`fix/env-manifest-page-managed-tenant`，基线`436b4f7b`。

## 改了什么

`ops/environments/env-41.toml`删除优链充(`1941847786056323072`)的
`[[agents]] ops-youlian-order-diagnosis`与`[[dify_apps]] app_id=349a637c-…`。
该入口2026-10-29经Dify页面“复制并开通”后已由`operator_apps`管理，页面发布的是`a57602c1-…`。

`tests/dify/test_dify_app_registry.py`新增`test_a_page_managed_entry_freezes_the_whole_manifest`：
守卫此前**零测试覆盖**。新测试断言三件事——整份清单在写入前中止（别的租户的行也没被写）、
页面选择未被覆盖、清单不再声明后同一注册表可正常收敛且`--prune`不删页面管理的行。

## 本地复现与验证

用真实仓库清单 + 按41回读状态复刻的注册表（三条`dify_app_registry` + 一条`operator_apps`）：

| 清单 | 结果 |
| --- | --- |
| 改动前 | `REFUSED: 页面管理的管家不能由环境清单覆盖` |
| 改动后 | `unchanged` ×2（两家现存绑定），无写入 |

变异检验：把守卫的`raise`改成`pass`，新测试报
`DID NOT RAISE ManifestError`；用内存里的原文整份写回，复绿。
不使用`git checkout`撤销，避免连带回退同文件其他未提交改动。

相关套件`tests/publication/test_agent_manifest.py`、`tests/dify/test_dify_app_registry.py`、
`tests/dify/test_dify_registry_blast_radius.py`、`tests/dify/test_dify_publish_api.py`：
**62 passed**。`ruff check`、`ruff format --check`（337 files）、`node .sandcastle/policy-check.mjs all`通过。

## 41 上的只读核对与一处归因修正

在41的**副本**上跑`aiops-admin reconcile --dry-run`（清单放`/tmp`，未动
`/opt/aiops-41/ops/environments/`）：**收敛中止：模型不在白名单**。用**改动前的清单**跑同一
dry-run，输出逐字相同 ⇒ 这一条与本次改动无关。

原因：`_preflight`的模型白名单校验排在`reconcile`与`reconcile_dify_registry`之前，而41的
`AIOPS_PROVIDERS`已是`deepseek-v4-1-flash-261017`，清单与**库里现役**的4个agent仍是
`deepseek-v4-flash`（逐条回读一致，不是清单写错）。这是既有配置漂移。

**这修正了#1171回执里的归因**：那里写"对该清单执行reconcile会在写入前中止"并归给守卫；
实际先中止的是模型白名单。守卫确实会让它中止（本地已证明），但41上当前看不到守卫那一步。

## 未完成

- 守卫修复**未在41端到端验证**：先要解决模型白名单漂移，那会改变线上行为（改白名单或升级
  4个现役agent的模型），属需单独决策的变更，本轮不动。
- 未跑全量测试，未做真实发布/切版回归；本次改动只影响清单文本与一处测试。
