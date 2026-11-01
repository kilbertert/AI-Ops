# T13 第 D 项：原入口双端复验

日期：2026-11-01（UTC 14:20–14:45）。分支 `docs/d-entry-reverify-minimal`，基线 `833e15b7`
（验证时线上 Gateway `1.0.0+9e456e918181`）。目标：把 A–L 里 D 行剩下的
「切换前后的原入口双端复验」补齐。

## 提交

- `docs/validation/2026-11-01-t13-d-dual-entry-reverify.md`（证据全文）
- `docs/validation/2026-11-01-t13-step5-channels-and-al-matrix.md`（D 行与统计说明）

## 结论

**路由与登录这一半通过，业务回答这一半两端都失败。**

| | 管家端 | 消费者端 |
| --- | --- | --- |
| 真实登录 | 通过 | 通过 |
| 落在正式绑定 | `operator_order_diagnosis` rev 12 | `consumer_service` rev 14 / v13 |
| 本轮结果 | failed `OPERATOR_DIAGNOSIS_FAILED` | failed `CHATFLOW_ANSWER_INVALID` |
| 原因 | 运行 manifest `tools=[]` → 节点无法调 `cpms_site_search` → 网关按 `OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收（4/4 复现） | 偶发解析不出合法 JSON（`json_syntax`）；同租户 170 条里 17 条失败（10%）；同原因按语言是 zh 11/113、en 1/33——样本太薄，不判语言是否成因 |

D 仍判 **部分**，但与上一轮不同：**未通过的原因从"没做"变成了"做了并查出两个具体问题"**。

## 一条自己踩的坑

初版把消费者端的失败分布写成「`en` 29 次全成功、13 次失败全是 `zh`」，据此暗示语言相关。
改的时候又过头了：拿混算的 `en` 4/33、`zh` 13/113 反过来说「与语言无关」，同样站不住——
那 17 条失败有三个不同原因，混在一起比没有意义。按同一原因拆开是 `zh` 11/113、`en` 1/33，
`en` 的分子只有 1，样本太薄。**两个结论都不成立，只能记观测、不判成因。**

两次都跟计数有关：第一次是 `| tail -10` 截掉了 `en failed` 那一行；第二次是没按原因分层。
计数先出全表、再分层，两件都要做。

另：`json_syntax` 只表示「输出以 `{` 或 `[` 开头但解析失败」，不等于模型没吐 JSON；
截断、转义错误、尾逗号都落在这一档。

## 与在办工作的关系

消费者端那条（解析不出合法 JSON）已由 PR #1291（已合并 `833e15b7`）加了**形态**标记
（`plain_text:<n>` / `fenced` / `json_syntax` / `empty` / `not_text:<type>`），只改诊断不改根因。
本 PR 只证明"原入口会发生"并给出频度与语言分布，不重复修。

## 一处没下结论的地方

管家端 `tools=[]` 有两个可能来源（模板 `manifest_file` 的工具表为空 / Dify 侧没勾
`cpms_site_search`），需要到 36 的 Dify 控制台看实际勾选才能定。本记录只写定位路径，
不写根因断言——`cpms_site_search` 的声明本身是对的（`applies_to:["conversation"]`）。

## 交付索引

`docs/agents/current-delivery-state.md` 顶部已补本轮结论：路由通过、两端回答失败，
并明说 2026-10-31 那条「两端原 H5 正例通过」**不能当作当前可用性**。

`pre-push` 的冲突闸报过 `branch-conflict`（该文件正被 `docs/youlian-operator-tenant` 与
`fix/dify-handoff-acceptance` 改）。读闸的源码：它只**打印**提示、不阻断推送，
所以照常提交；那两个分支若先合，本 PR 会有文本冲突，按本节结论取舍即可。

## 清理

隧道、浏览器会话态、验证码临时文件均已删除；订单号与原始响应对公 PR 脱敏。
