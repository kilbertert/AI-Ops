# 提示词生命周期进度

续办worktree为AI-Ops-prompt-lifecycle-browser，分支test/prompt-lifecycle-browser，承接PR #1232的e10b1142。
客户端指定SYSTEM与管家原编排页已实跑编辑/预览/发布/恢复；管家4次真实Java执行成功，
身份/CPMS/模型合成。已补事实反转、管家事实段、否定执行声明及旧协议兼容。
最终联合页面119/119通过；全量3759 passed / 9既有跳过，构建和静态检查通过，已分片本地提交。
真实模型最终统一16格15通过，账单片段行内前缀被拒；保留失败，不称稳定性通过。

## 第四轮复核（2026-10-31）

四项实际反例已修：A 否定改写状态、B 英文正常追问被误拒、C 管家英文事实标签、D 行内前缀。
证据见 `docs/validation/2026-10-31-prompt-lifecycle-r4-fixes.md`（29 专项 + 35 验收脚本 + 全量 3764）。

## 第五轮：并入主线并解锁推送（2026-10-31）

原交付 worktree 的重叠阻塞已由执行者以 `a6baf88b` 提交解除。本地交付链
`19a4b05b`（= `77f97f30` + `e0341551` + `a6f61d9c` + 流程提交）与远端 PR 分支是**平行**的两条线，
内容等价；远端 PR 分支 `77e33678` 已自带 `a6f61d9c`，故以其为基线继续，而不是换分支。

随后两次对齐受保护基线，均在最终树上重建 `images.lock.json`：

| 合并 | 主线 | 全量 |
|---|---|---|
| `ad11fe91` | #1238 | 3798 passed / 9 skipped / 0 failed |
| `3a381923` | #1239 | 3803 passed / 9 skipped / 0 failed |
| `51b9ff95` | #1240（受管工作流页） | 3803 passed / 9 skipped / 0 failed |

`context_digest.py --check` 与 `source_digest.py --check` 在最终树上均 `match=true`。

下一步：推送同一 PR 并等待新 head 的必需 CI；再做固定两批模型样本、受管 UI 页面验收、
41 原应用/原 H5 验收。仍未完成业务验收。
旧回执中的“缺译文/缺冻结清单”已纠正，复用consumer_language及execution_manifest。

## 第六轮：生产启用 slots（2026-10-31）

开工核对（只读，41 服务身份）：`main=68ab9207`，41 `/health=1.0.0+4ec55c7a1c0c`；
草稿 `7847fe49`（hash `02241270…`，sha256 `ff5c5d33…`）、发布 `d0b82590`（2026-10-30 10:07），
14 节点、3 个环境项 —— 与离线候选基线完全一致，无漂移，候选 `6b33b7cb…` 复用有效。
C 端模型参数：`max_tokens=1000`、`enable_thinking=false`、`temperature=0`（草稿与已发布一致）。

已纠正交接两点：`--live-documents` 走 `live_documents`、不整图替换（上一版回执判断有误）。
下一步：旧 SYSTEM 技术协议冲突验证 → 真实模型预览 → 发布 → 正式新会话 → 恢复。

## 第七轮：失败取证（2026-11-01）

生产订单查询报 `reason=CHATFLOW_ANSWER_INVALID`、`reject_detail=invalid document answer`,
`data_kind=status`，落点在 `consumer_documents.answer_selection` 的 `json.loads` ——
即模型这一轮**没有输出 JSON**。要修就得先知道它输出的是什么形态。

- #1288：`reason` 保持稳定错误码，新增 `reject_detail` 记具体判据（已部署）。
- 本轮：在 `json.loads` 失败处补**形态**（`empty` / `fenced` / `json_syntax` /
  `plain_text:<长度>` / `not_text:<类型>`），**不记原文** —— 原文是用户可见文本，
  可能含订单号金额。详见 `docs/validation/2026-11-01-answer-shape.md`。

下一步：部署后重跑订单查询（真实用户会话 `07b9229f…`、订单 `2109928861763776513`），
按读到的形态决定改协议块约束、解析端容忍围栏，还是调 `max_tokens`。
**业务验收未完成。**

## 第八轮：订单链路的真因（2026-11-01）

#1291 部署后（`1.0.0+833e15b7b5d8`）重跑订单查询，第一条就拿到形态：
`invalid document answer (json_syntax)`。接着在 Dify console 侧逐条 `json.loads`
12 条答复，4 条不通过，理由**逐字相同**：正确的对象之后多了一个 `"}`。
不是截断（失败条目比成功的最短条目还短），不是提示词（同一份提示词 30 秒前刚成功）。

`load_selection` 因此容忍"完整对象 + 标点尾巴"，并保留严格边界（见
`docs/validation/2026-11-01-trailing-brace.md`）。部署后再跑同一订单，
要求连续多次成功、且 `reply_source` 真是模型写的那一份。

**业务验收未完成。**
