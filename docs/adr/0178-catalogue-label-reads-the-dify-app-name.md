# 178. 来源下拉的名字读 Dify，不在登记时存

日期：2026-10-30
状态：已采纳（已实现，未部署）

## 决定

`GET /v1/dify/management/catalogue` 的 `sources[].name` 由后端拼成
`客户名 / Dify应用名`，不再直接吐 Gateway 的智能体名：

| 字段 | 来源 | 取不到时 |
| --- | --- | --- |
| 客户名 | 公司 `/tenant/list/my`（与可开通客户目录同源） | 省略，只显示应用名 |
| Dify应用名 | 按应用所属 workspace 读 `/console/api/apps/<id>` | 退回 Gateway 智能体名 |
| 版本号 | `chat_route_bindings.agent_version_no` | `V` 是**智能体**正式版本，不是 Dify 应用版本 |

应用名按 `app_id` 缓存 **120 秒**（进程内）。读名失败**不影响目录**：`platform`、`tenants`、
`sources`、`operations` 照常返回。标签只用于显示，`create` 仍以 `source_app_id` +
`source_version` 为准，名字不参与任何判据。

## 依据

运营在下拉里要回答的是「这是谁的哪个应用」。来源应用全部住在**客户工作区**
（`aiops-41-Ulink-…`、`aiops-41-优链充-…`），平台账号在大厅里看不到它们，只有「进入
工作区」才会被临时加进去；而 `operator_apps.agent_name` 里存的是 `operator-<app_id>`、
`ops-methodology-700` 这类执行侧标识。两者叠加的结果是：下拉里两个条目**无法对照任何
运营能看到的东西**。这不是文案问题，是答不出「这是哪来的」。

前端只读 `sources[].name`（`deploy/dify-36/web-patches/1.17.1/aiops-management.tsx`），
所以标签在后端拼好即可生效 —— 不动 Dify web 镜像。镜像重建与重新分发是这条链上最贵的
一步，能不动就不动。

## 被否掉的替代形态

**登记时把应用名写进 `operator_apps.agent_name`。** 最强的地方是读路径零外部调用，
一次写入长期有效。它输在两处：`agent_name` 已经是执行侧标识（41 上那行
`agent_name='ops-methodology-700'` 是 `managed-adoption` 写入的既有事实，改名要动它）；
而且客户在 Dify 里改了应用名之后，库里那份快照就成了没人知道它过期的陈旧值。

**列全 workspace 应用再映射（`/console/api/apps?page=…`）。** 最强的地方是一次调用
覆盖多个来源。它输在成本：一个 workspace 的翻页上限是 50 页，而来源通常只有一两个，
按需读单应用更省。

**读名失败让目录失败（fail-closed）。** 最强的地方是绝不显示错名字。它输在入口可见性：
目录是整个开通面板的门 —— 前端在 `platform` 为假时**整块静默消失**，让一个装饰性字段
决定能不能开通，是把这个静默失败面扩大。

**改前端文案（`客户 / 应用 · 智能体V`）。** 最强的地方是文案完全可控。它输在交付成本：
改 `aiops-management.tsx` 要重建并重新分发 web 镜像，与上一条依据正相反。末位 `V` 的
语义改为文档记录（`docs/agents/operator-cpms/self-service.md`），不改字面。

## 后果与验证

- 缓存未命中时目录多一次外部读。超时与异常已被吞掉，但**延迟**要盯：120 秒 TTL 是按
  「工作室每打开一次就拉一次目录」定的，不是按应用名变化频率定的。
- 缓存是进程内的：重启或多副本各自冷启动。它只影响标签新鲜度，不影响授权、开通或路由。
- 客户名缺失时标签退化为只用应用名，不显示 `None`/空占位；`sources[]` 仍带
  `tenant_id`/`tenant_name`/`app_name` 三个结构化字段，供将来前端改文案。
- 验收：`tests/dify/test_self_service.py` 两条 —— 组合标签、且第二次读目录不再问 Dify；
  读名抛错时目录仍 200 并退回智能体名。真实控制台读取与真实平台下拉**未验**，
  见[验收记录](../validation/2026-10-30-catalogue-source-labels.md)。
