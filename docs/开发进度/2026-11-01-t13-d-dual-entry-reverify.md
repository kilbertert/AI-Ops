# T13 第 D 项：原入口双端复验

日期：2026-11-01（UTC 14:20–14:45）。分支 `docs/d-entry-dual-reverify`，基线 `833e15b7`
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
| 原因 | 运行 manifest `tools=[]` → 节点无法调 `cpms_site_search` → 网关按 `OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收（4/4 复现） | 偶发解析不出合法 JSON（`json_syntax`）；同租户 170 条里 17 条失败（10%），按语言无差异（`en` 4/33、`zh` 13/113） |

D 仍判 **部分**，但与上一轮不同：**未通过的原因从"没做"变成了"做了并查出两个具体问题"**。

## 一条自己踩的坑

初版把消费者端的失败分布写成「`en` 29 次全成功、13 次失败全是 `zh`」，据此暗示语言相关。
实际 `GROUP BY` 全表是 `en` 4/33、`zh` 13/113——**同量级，与语言无关**。原因是计数时用了
`| tail -10`，把 `en failed` 那一行截掉了。计数只用全表，不用管道尾部。

## 与在办工作的关系

消费者端那条（解析不出合法 JSON）已由 PR #1291（已合并 `833e15b7`）加了**形态**标记
（`plain_text:<n>` / `fenced` / `json_syntax` / `empty` / `not_text:<type>`），只改诊断不改根因。
本 PR 只证明"原入口会发生"并给出频度与语言分布，不重复修。

## 一处没下结论的地方

管家端 `tools=[]` 有两个可能来源（模板 `manifest_file` 的工具表为空 / Dify 侧没勾
`cpms_site_search`），需要到 36 的 Dify 控制台看实际勾选才能定。本记录只写定位路径，
不写根因断言——`cpms_site_search` 的声明本身是对的（`applies_to:["conversation"]`）。

## 清理

隧道、浏览器会话态、验证码临时文件均已删除；订单号与原始响应对公 PR 脱敏。
