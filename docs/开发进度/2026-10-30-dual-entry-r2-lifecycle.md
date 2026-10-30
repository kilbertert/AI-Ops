# 双端开通第二轮：打通真实链路、修状态语义、补依赖与知识边界

日期：2026-10-30（第二轮）。分支 `feat/dual-entry-r2`。基线 `4403681e`。
依据：[独立验收退回](../acceptance/dual-entry/INDEPENDENT-REVIEW.md) 与
[返工任务书](../acceptance/dual-entry/CLAUDE-REWORK.md)。

## 目标

第一轮的 10 个独立反例全部失败。本轮把"看起来能跑"换成"真实路径能跑"：

- 页面发出的双端 payload 必须能穿过**真实同源桥**（R1）；
- 客户端导入 id 必须满足**真实 Dify 补丁**的约束（R2）；
- 未发布草稿能真的试用、第二份草稿能真的发布、发布必须核对页面看到的草稿摘要（R4—R6）；
- 重试不覆盖人工决定、凭据对账跨重启幂等、首次发布失败能续跑（R7—R9）；
- 对客/内部知识有**显式授权**，撤销后已发布版本也不再检索（R10）；
- 客户端依赖与业务连接**开通即就绪并可回读**，缺条件就拒绝而不是谎报（R11）。

## 已完成内容

| 项 | 落点 | 关键决定 |
| --- | --- | --- |
| R1 | `dify/self_service_bridge.py` | 白名单加 `scope`/`sources`，**同时**逐字段校验形状；桥是固定动作契约，不做透传 |
| R2 | `dify/self_service_prepare.py` | `request_id = uuid5(NS, "consumer:"+job)`（满足 `^[a-f0-9-]{36}$`），应用 id 按补丁同一条派生 `uuid5(NS, tenant+":"+request_id)` |
| R4 | `gateway/gateway_api.py` | 只对**正式**路线要求 `active`；草稿要求"仍属于这个客户"（由 `require_current` 的草稿分支查目录） |
| R5 | `self_service_store.py`、`consumer_publication.py` | 目标按 `(租户, 环境, **应用**)` 取；文件与受管行**同一应用**各说一套才叫冲突，不同应用不冲突 |
| R6 | `dify/self_service.py` | `status` 返回 `draft_sha256`；客户端发布核对它，不一致即 `SETUP_DRAFT_CHANGED` |
| R7 | `dify/self_service.py` | 指针已是本应用时**只对账不推进**；撤回状态不会被重试改回 active |
| R8 | `dify/service_keys.py` | 创建前快照写进 `service_key_ops` 表；跨请求/重启用差集对账，不建第二枚 |
| R9 | `dify/dify_dsl_pull.py` | `bootstrap` 允许接管**配置逐项相同**的未发布草稿并续跑；不同的草稿一律不动 |
| R10 | `consumer/consumer_knowledge_grants.py` | 显式 `(租户, 环境, 库)` 授权行；发布与检索都查；撤销即时生效；按名字复用改为**每次新建** |
| R11 | `dify/self_service_prepare.py`、`consumer/consumer_knowledge_grants.py` | 业务连接从**已批准环境连接**登记（无则拒绝该端）；插件+目录凭据写入并回读；源编排的每个模型必须有凭据 |
| 结构 | `dify/self_service.py` | 模型/插件/节点 provider 从"公共准备"移到**各自端**：只开客户端不再依赖管家 provider |

## 新增回归

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/dify/test_dual_entry_bridge.py` | 10 | 双端/单端 payload 穿过真实桥；5 种畸形 payload 仍被桥拒 |
| `tests/dify/test_dual_entry_lifecycle.py` | 8 | R2 导入 id（用补丁正则校验）、R4 草稿试用、R5 第二份草稿、R6 摘要、R7 重试撤回、R8 跨请求对账、R9 续跑、冻结同一份内容 |
| `tests/consumer/test_consumer_knowledge_grants.py` | 6 | 内部库发布被拒、授权库通过、缺字段 fail closed、登记按租户/环境隔离、撤销后已发布版本被拒 |
| `tests/dify/test_consumer_dependencies.py` | 8 | 已批准连接登记/按租户映射/无连接拒绝、插件凭据幂等、模型凭据回读、未批准 provider 拒绝 |

## 验证

```bash
uv run pytest -n 0 tests/dify/test_dual_entry.py tests/dify/test_dual_entry_bridge.py \
  tests/dify/test_dual_entry_lifecycle.py tests/dify/test_consumer_dependencies.py \
  tests/consumer/test_consumer_knowledge_grants.py tests/consumer/test_consumer_draft_trial.py
# 57 passed
uv run pytest -n 4     # 3657 passed, 9 skipped
uv run ruff check src tests && uv run ruff format --check src tests
```

`deploy/m2-live-03/images.lock.json` 重建写回（`source_sha256=d90202de…`）。

## 未完成（见 BLOCKED 与回执）

- R3 真实 Chatflow 页接入与首页入口：见同日前端里程碑。
- R12 Dify web 镜像部署、桥/API 部署与渠道声明。
- R13 原 CI 间歇失败的语义判定。
- 真实 A–L 双租户验收。
