# 管家端没有「客户案例」动作：入口面由**能力**决定，不由数据补齐决定

日期：2026-10-16
状态：已采纳
影响版本：`shortcut_lifecycle._BUNDLED_SHORTCUTS["operator"]`、`docs/agents/frontend-operator-handoff.md`、
`docs/agents/env-41-runbook.md` §3.1-sexies；收窄 #427 的交付面

## 背景

`operator`（管家端）入口自 #427 起带一个 `case_exploration`（「客户案例」）动作。
2026-10-15 在 41 生产上实测，它**从来没有出过卡片**：

| 时刻（UTC） | 入口 | `retrieval_status` | `duration_ms` |
|---|---|---|---|
| 02:03:03 / 09:37:19 / 11:17:58 / 11:21:17-26（共 7 次） | operator | `unavailable` | **9–13** |

反向对照：10-14 17:16:42 同一条路**真跑成功**的那次是 `found` + **41951 ms**。
真人跑一次要几十秒，`9–13ms` 只可能是**根本没启动**。

## 决定

**把 `case_exploration` 从 `operator` 种子里删除**，不再向该入口提供这张卡。

理由是**这个入口结构上产不出它**，不是素材缺口：

- `target_agent_version`（指向宣传智能体的那一跳）**只允许落在租户行**，
  平台行被显式拒绝（`ShortcutManager.create` / `update` 对 `PLATFORM_SCOPE` 抛
  `platform shortcuts cannot bind a tenant agent version`）；
- 而 `operator` 侧**只有平台行**（租户覆盖行从未建过）；
- ⇒ `_promo_route` 在该入口**永远**解析不出宣传智能体 ⇒ 每次点击都是一张
  「客户案例服务暂时不可用」。

在 41 上用**线上代码 + 线上库**直接跑过 `_promo_route` 与 `_published_promo_target`：
同一租户、同一问句，`consumer` 入口得 `agt_ed443cae…#v2`，`operator` 入口得 `None`。
⇒ 归因干净，不是猜。

**同时定下动作集**：`operator` 入口是 `smart_diagnosis`（订单检测）+ `operations_review`
（运维巡检）两个。运维人员要的是**名下站点的运行情况**与**单张订单的根因**；
客户案例是给终端车主的销售叙事，不是这个入口的职责。

## 被否掉的替代形态

- **(a) 给 `operator` 补一行租户覆盖行、绑一个宣传智能体。** 技术上完全可行（机制没有拦路：
  `_validated_fields` 不区分入口，`select_promo_agent` 只看租户）。否掉是因为**用户口径是
  「管家端用户不要看到客户案例卡片」** —— 补数据会让卡片出现，方向相反。
  另：现成的那个宣传智能体绑的库是重卡流量平台的**销售物料**，文案面向客户，
  给运维看是错的语境；补数据之前先得有面向运营商的素材。
- **(b) 在 `_promo_route` 里加一条「operator 入口一律不进宣传面」的代码级禁止。**
  更彻底（`operator` 的自由文本问「客户案例」也不再走宣传面），用户**明确否掉**：
  范围是**只摘按钮**。保留自由文本那条路，它仍会给出一句诚实的「服务暂时不可用」。
- **(c) 只把数据行 `disable`、代码里留着种子。** 否掉：`seed_bundled` 对**新**租户仍会
  播种这一行，于是每个新租户都要再手工 disable 一次 —— 把一个已判定不该存在的动作
  变成持续的人工负担。种子与数据行**两条都要改**：种子决定将来，数据行决定现在。
- **(d) 用 `rollback` 撤回 41 上那行。** 否掉：该动作只有 `version_no=1`，而 `rollback`
  的语义是「把某个旧版内容**重新发布**」——"回滚到 v1"就是再发一次同一份内容，
  入口照样返回它。正确动作是 `disable`（判据与既有 §3.1-ter 撤回节同一条）。

## 后果

- **前端产物仍会渲染那颗按钮**：管家端前端**硬编码了按钮**，不消费后端返回的列表
  （既有偏差，见 `frontend-operator-handoff.md` §6 末条）。⇒ 停用数据行后，
  **用户界面可能仍显示它**，点下去落进普通问答。这不是停用失败，要在前端改造里一起收。
- **未部署**：本记录只改代码与文档；41 上那行仍是 `published`，需按 runbook §3.1-sexies
  `disable`（写库属服务主机变更）。
- **`consumer` 侧零影响**：种子、数据行、问句模板均未动；本次只删 `operator` 块。
- 若日后产品改主意（要管家端出卡），恢复步骤三步记在 `frontend-operator-handoff.md` §3.2-bis，
  含内容侧前置 —— **光有知识库不会 `found`**。
