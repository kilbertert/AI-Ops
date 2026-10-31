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

`context_digest.py --check` 与 `source_digest.py --check` 在最终树上均 `match=true`。

下一步：推送同一 PR 并等待新 head 的必需 CI；再做固定两批模型样本、受管 UI 页面验收、
41 原应用/原 H5 验收。仍未完成业务验收。
旧回执中的“缺译文/缺冻结清单”已纠正，复用consumer_language及execution_manifest。
