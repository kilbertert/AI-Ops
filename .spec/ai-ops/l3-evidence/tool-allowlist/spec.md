---
title: tool-allowlist
desc: 节点工具按当前任务清单授权
code:
  - src/aiops_diagnostics/operator_cpms.py
related:
  - src/aiops_diagnostics/execution_manifest.py
  - src/aiops_diagnostics/node_publication.py
---
# tool-allowlist

## raw source

旧七工具及共享租户 provider 已退役；管家节点使用 CPMS 两工具，由任务绑定与发布清单共同约束。

## 不变量

- 任务、节点、公司主体、订单和发布版本必须一致，读前/完成/回读均复核。
- 模型不能提交 SQL、改租户或取得共享业务凭据。
- 空工具集合不自动恢复默认；草稿试用不推进正式发布指针。
- 结果引用本轮证据，不声称执行过退款/修改等业务动作。
