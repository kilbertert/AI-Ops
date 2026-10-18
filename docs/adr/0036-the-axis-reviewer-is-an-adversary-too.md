# 36. 另一条审查链路也拿到对抗授权（判据分两层）

日期：2026-10-18
状态：已采纳
影响版本：AFK 脚手架模板 1.7.25（`.sandcastle/review/axis-prompt.md` 新增 `# YOUR JOB`）；
不改任何运行时代码

## 背景

本仓有**两条**审查链路，都是审查者：

| 链路 | 加载的 prompt | 1.7.24 覆盖 |
|---|---|---|
| 本机 ralph（`planner.ts:292`） | `.sandcastle/review-prompt.md` | ✅ |
| Actions `agent:review`（`review.ts:178`） | `.sandcastle/review/axis-prompt.md` | ❌ |

1.7.24 把对抗授权交付到了第一条，并在其 ADR 里**明确考虑、且正确地否掉**了
`.sandcastle/review/prompt.md`（两轴之后的修复者，职责不同）。那段推理漏掉的是：
**同一个目录里还有 `review/axis-prompt.md`，它才是另一条链路上的首轮审查者**。

于是这条纪律到了一条审查链路，没到另一条。**这是本机 AFK 脚手架第四次交付
「规则只发到作者想到的路径」这个缺陷**（前三次：回写纪律只覆盖 5 条 implement prompt
里的 2 条；审查面只写到 scaffold、消费仓 0 of 3；补下发时漏掉同目录的第二个文件）。
afk-bootstrap 1.7.25 补齐。

## 决策

**本仓的 `.sandcastle/review/axis-prompt.md` 增加 `# YOUR JOB` 一节，判据分两层：**

- **能示范就示范**：`If you can reproduce a failure by running something … do that and
  report the output; a demonstrated failure beats a described one.`
- **不能示范时**，反例必须精确到可被检查：`"The input X reaches this branch, and the
  guard above it only covers Y" is a finding. "This looks fragile" is not — it names no
  input, so nobody can check it.`

以及：**全绿的 suite 不是证据**（这个 diff 里的测试是作者写的，编码的是作者的假设），
若真试过仍构不出反例，就说试了什么。

**「只读」的边界**：这个 prompt 的正文禁止 `Do not edit files, commit, push, or reply
to GitHub comments`——但**不禁止执行**。`review.ts` 与其它链路共用 `claudeProfile`，
`onSandboxReady` 会跑 `sandbox-prepare.sh`，所以它有 sandbox。这一节写明了它**可以**
跑东西来复现失败，只是不能改。

## 被否掉的替代形态

- **照抄 `review-prompt.md` 的那一段。** 那句判据是「一条 finding 是你**跑出来**的失败输出」，
  而这个 prompt 的输出是 JSON `findings`，且常需要在**不能实际复现**的情形下下判断
  （缺失的上游、生产数据）。照抄会把「跑出来」当成唯一合格形式，逼它假装跑过，
  或让有价值的描述型反例被自我否决。

- **断言这个审查者「不能执行」。** afk-bootstrap 的第一版这么写了（`You cannot run the
  code`），核实后是错的。这条若留着会**劝阻审查者去复现它本来能复现的失败**，
  比不写更坏，因为它披着「我只读所以只描述」的合理性外衣。

- **只补 scaffold，等下次自然拿到。** 那正是 1.7.23 的错（消费仓 0 of 3）。
  已存在的仓恰是**正在跑** AFK 的那些。

## 后果

- 两条审查链路现在都有对抗授权，且各按自己的能力措辞。
- **独立性仍然有限，必须说清**：它来自新上下文与这条授权，**不来自模型差异**——
  本机所有 AFK 项目共用一套模型路由。
- 本决策**只改审查 prompt，不改任何运行时代码**，不影响服务行为。
- 代价：仓库里现在有两段措辞相近、判据不同的对抗文本。这是刻意的——合并它们会让
  另一条链路背上做不到的标准。两段都写明了理由，读者不会以为是复制粘贴遗留。
