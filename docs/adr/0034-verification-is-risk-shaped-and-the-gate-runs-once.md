# 验证按风险成形，全量门禁只跑一次

日期：2026-10-18
状态：已采纳
影响版本：AFK 脚手架模板 1.7.23（`.sandcastle/*` 的 prompt 与 `CODING_STANDARDS.md`）；
不改任何运行时代码

## 背景

AFK 的三个实现 prompt 里长期写着这一段，且它直接坐在验证门禁之上：

```
Use red-green-refactor where applicable:

1. RED: write one failing test
2. GREEN: implement to pass it
3. REPEAT until the issue is done
4. REFACTOR the code

Before committing, run `uv sync --extra dev && uv run pytest && uv run ruff check`
```

**它不是"测试约定"，它是一个循环。** `3. REPEAT until the issue is done` 读起来就是
"一直转"，而紧跟其后的 gate 是**全量** `uv run pytest` + ruff。所以 agent 实际读到的
指令是「跑完全部，然后再来一轮」。

本仓实测：

| | 耗时 |
|---|---|
| 全量 `uv run pytest`（1805 个测试文件） | **299.62 s** |
| 单文件 `tests/test_cli.py` | **2.91 s** |
| 倍数 | **≈103×** |

`.sandcastle/logs/issue-92..99.log` 显示该 gate 每个 issue 被调用 **4 次以上**。
一个 issue 因此在反复重跑它已经跑过的 suite。**写测试不是成本，重复才是。**

这一点值得说准：**本仓从未启用任何覆盖率阈值**——`pyproject.toml` 无 `fail_under`，
CI 不验覆盖率，`.sandcastle/policy-check.mjs` 也不读覆盖率。压测出来的不是"测试太多"，
而是"同一个门禁在一轮里被要求跑很多遍"。

## 决策

**重写承载循环的那一个区块：保留"测试是最便宜的 oracle 时先用测试"，把全量门禁从
"每一轮"降为"收尾一次"。**

新文本要求：迭代时只跑**能证伪当前改动的最窄检查**（改动涉及的测试文件、typecheck、
linter）；全量 gate 在收尾跑一次。同时明确**不为凑数字造测试**——散文、格式化、
改名、一行接线不欠测试；要能说出"哪个输入会弄坏这次改动"，说不出来就在 commit body
里说，而不是写一个把实现断言回自己的测试。

`CODING_STANDARDS.md` 的 `Update tests alongside behavior changes; keep the suite green.`
改为：**回归测试是老行为仍然成立的持久记录**（这条被强化，不是削弱），而全量 suite 是
**交付前的门禁，不是每次编辑后转的圈**。

## 被否掉的替代形态

- **整份照搬外部的「从此不写测试、既有红灯不再是事故」doctrine。** 与
  `.sandcastle/consensus-contract.json` 把 `deterministic-ci` 列为不可豁免不变量的做法
  直接冲突：红灯 = CI 红 = 合不了。照抄会自断门禁。其中**「作者 ≠ Hacker」**（对抗性验证
  必须换手）被采纳，因为本仓 implementer 与 reviewer 是同族模型、同一套 prompt，
  独立性本就弱。

- **维持 blanket TDD，只把全量换成分片。** 治不到「REPEAT」这个结构：`3. REPEAT`
  读起来就是无限循环。103× 的比值说明省下的是**重复次数**。

- **按依赖链自动推断该跑哪些测试。** 本仓测试与实现并非一一对应，自动推断会**漏**——
  漏掉的那次是静默的，比多跑一次贵得多。所以把范围判断交给改动者，并要求它说出
  哪个输入会弄坏改动。

- **用整文件覆盖（`cmp` 快照）下发这条纪律。** 本仓的 live prompt 与模板已差 9–25 行，
  原因包括手工套用的语言替换（模板写 `npm run check`，本仓是 `uv run pytest`）
  与本仓手写的验证 skill 段落（`verify-aiops-gateway`、一次性数据根、设备 token 红线）。
  覆盖会销毁那段，而"守卫拒绝"又会让升级在本仓静默 no-op。改用锚点编辑。

## 后果

- 一个 issue 在循环里的全量门禁次数从 4+ 降到 1。按上表，单 issue 省下约 15 分钟。
- **回归测试的地位被强化，不是削弱**：`test_http_auth`、`test_faq`、`test_health_report`
  这类真实行为测试一个没动，`CODING_STANDARDS.md` 那句反而从"keep the suite green"
  写成了"回归测试是老行为仍然成立的持久记录，正因为这个理由才值得写"。
- 本仓手写的验证 skill 段落与设备 token 红线**原样存活**。
- **代价**：迭代时只跑"最窄检查"依赖改动者判断范围，判断偏了会漏；补偿是 CI 仍跑全量，
  且 prompt 要求说出会弄坏改动的那个输入，而不是自己给自己发绿灯。
- 本决策只改脚手架 prompt，不改任何运行时代码，因此不影响服务行为。
