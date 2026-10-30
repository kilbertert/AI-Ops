# 客户双端开通：目录、客户端准备与逐端发布

日期：2026-10-30。分支 `feat/dual-entry-onboarding`。规格见
[docs/acceptance/dual-entry/implementation-spec.md](../acceptance/dual-entry/implementation-spec.md)，
验收映射见 [qa-plan.md](../acceptance/dual-entry/qa-plan.md)。

## 里程碑目标

让"平台给一个客户开通一套 AI 服务"从**只认管家**变成**默认双端**：一次提交里客户端客服与
管家各自准备、各自发布，一端失败不牵连另一端；客户端应用自动准备（应用、依赖、凭据、
知识引用），客户端的发布目标从逐客户手改文件变成受管运行数据。

## 已完成内容

| 子任务 | 内容 | 落点 |
| --- | --- | --- |
| T1 | 实施规格、验收合同（A–L）、子任务依赖、QA 映射 | `docs/acceptance/dual-entry/` |
| T2 | 端/场景词表与渠道口径；受管目录区分端/场景/环境/目标 | `dify/dual_entry.py`、`dify/self_service_store.py` |
| T3 | 目录 API 返回 `entries[]`（每端各列来源），顶层 `sources` 保持管家端兼容 | `dify/self_service_api.py` |
| T4 | 开通任务 `scope`/`sources`、逐端步骤与状态、`partial`、重试续跑 | `dify/self_service.py` |
| T5 | 客户端应用复制：来源已发布快照、知识选择整段重绑、幂等导入 | `dify/self_service_prepare.py` |
| T6 | Service 凭据：先快照、再创建、再核对；丢回执对账；0600 原子落盘 | `dify/service_keys.py` |
| T7 | 受管客户端运行目标为第一权威，现役文件保留、冲突显式拒绝 | `consumer/consumer_publication.py` |
| T8 | 未发布副本可试用（合成 draft 路由，不写正式指针） | `consumer/consumer_trial.py`、`gateway/gateway_api.py` |
| T9 | 逐端发布/撤回/恢复，复用控制台发布同一条推进路径 | `dify/self_service.py` |
| T10 | 渠道状态读环境清单 `[[channels]]`；四件事分开报 | `dify/dual_entry.py`、`gateway/gateway_config.py` |

## 关键决定

1. **受管目录不新开表。** `operator_apps` 加 `entry`/`scenario`/`environment`/`targets_json`
   四列，存量行按默认值补齐为管家，与现状逐字一致。新开表要迁移两次，收益为零。
2. **目录与正式指针仍然分离。** 目录说"这个应用属于谁、是哪一端"，`chat_route_bindings`
   说"这个端此刻由哪一版服务"。发布才推进指针；复制不替换现役正式版。
3. **客户端首次发布就是 V1。** `sync_workflow_from_dify` 增加 `bootstrap` 参数：agent 不存在
   时用**已经导出的那份文档**建配置，再在同一个版本里冻结编排。先建配置版再发编排版会让
   客户的第一个正式版本号不是 V1。
4. **知识引用整段替换，不做映射。** 映射没命中时最自然的实现是"保持原样"，而原样就是源客户
   的库 ID。整段替换让"源 UUID 留在副本里"在结构上不可能，再补一道 `forbidden_ids` 检查。
5. **凭据先快照后创建。** 只按"创建前没有、创建后有一枚"不够：创建前如果已有别人的 key，
   创建后取第一枚就会拿到别人的。快照按 `id+token` 记，新增必须恰好一枚。
6. **渠道状态不推导。** 不从"有应用、有正式版"推出"用户能用"，只读环境清单声明；
   没有声明就是待接入。
7. **两端不是同一个事务。** 一端失败另一端继续，整体 `partial`，重试只续跑失败端。

## 验证结果

- 新增 `tests/dify/test_dual_entry.py`（26 项）与 `tests/consumer/test_consumer_draft_trial.py`（3 项）。
- 全量 `uv run pytest -n 4`：**3620 passed / 9 skipped**（既有跳过，无新增跳过）。
- `uv run ruff check src tests` 与 `uv run ruff format --check src tests` 通过。
- 回归中发现的真实缺陷：`node_publication.all_targets` 遍历受管目录全部行，客户端行的空
  `profile_id` 会让**整个**目标列表加载失败（`KeyError: ''`）——一个客户端应用顺手把管家端
  全部停掉。已改为只读 `entry='operator'` 的行，并由
  `test_both_entries_are_created_and_published_separately` 固定。
- `deploy/m2-live-03/images.lock.json` 已按仓库流程重建镜像并 `--update-lock`
  （`source_sha256=6605528f…`，`image_id=sha256:b66c32ed…`，`built_at=2026-10-30T11:34:16Z`）。

## 已知缺口

- 前端（Dify 编排页/工作室的双端入口与逐端进度）在下一片交付，本片只有 API。
- 真实双端验收（真实消费者/员工、原页面、QA/run/版本关联）未执行，见
  [PROGRESS.md](../acceptance/dual-entry/PROGRESS.md)。
- 管家端仍不支持带知识库发布（既有约束，本片未改）。

## 下一步

前端双端入口 → 部署到 41 → 真实双端验收（A/B 页面开通、E 页面发布/撤回、G/H/I 真实会话）。
