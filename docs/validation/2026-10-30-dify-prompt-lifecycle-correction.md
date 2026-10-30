# 提示词生命周期：复核缺陷修复记录（更正首轮结论）

日期：2026-10-30
分支：`feat/dify-prompt-lifecycle`（PR #1223，未合并）
复核基线：`b634e790`；独立复核报告与反例在私有目录
`~/.cache/aiops-prompt-lifecycle-review-1223/`（`review.md`、`probe.py`、`probe-result.json`）。

**首轮结论是错的。** 首轮写"四类错话全部拦住、两个契约一致、16 格通过"，复核用 10 条反例
证明：**放行**了标签互换、状态说反、编造站名、英文可用性断言，**误拒**了带单位的正确账单、
`结束后可查看账单`、`我已完成本次查询`；documents 契约的 clarify/not_found/human 仍拒绝 `reply`；
版本"闭环"用例是 node fixture + 直接 `store.publish`，且脚本里写死 `pass`。

## 一、独立反例：10 项全红 → 10 项全绿

```bash
uv run python ~/.cache/aiops-prompt-lifecycle-review-1223/probe.py
# 修复前：10 cases; 10 failed（退出 1）
# 修复后：10 cases; 0 failed（退出 0）
```

| 反例 | 修复前 | 修复后 | 判据 |
|---|---|---|---|
| documents clarify/not_found/human 的 reply | 拒（non-document answer has content） | 接受 | `answer_selection` 放行三类，`validate_notice` 判"无数字/无链接/无执行声明" |
| 合计27.36元，付款20元，退款0元 | 误拒（货币数字被当成编造） | 接受 | 货币数字只要等于快照金额即合法 |
| 20是合计，27.36是付款 | 放行 | 拒（LABEL_MISMATCH） | 金额标签绑定**两侧**（值在标签前也算） |
| finished 说成"仍在充电中" | 放行 | 拒（STATUS_INVERTED） | 状态名取自公司状态表，必须与 `status_role` 一致 |
| charging 的"结束后可查看账单" | 误拒 | 接受 | 只有**完整状态名**算状态断言 |
| charging 的"我已完成本次查询" | 误拒 | 接受 | 同上 |
| 我帮你找到了虚构乙站 | 放行 | 拒 | 站名形状（以站/桩结尾）一律拒，不只比对已知站名 |
| The matching station is free and available. | 放行 | 拒 | 价格/距离/可用性的封闭词表（含英文） |

## 二、本次修复（逐条对应复核意见）

1. **真实客户端链路**：`consumer_documents.answer_selection` 允许 clarify/not_found/human 的
   `reply`（服务故障仍走系统文案）；`consumer_knowledge.final_answer` 归一化时不再丢 `reply`。
2. **事实判据换成"封闭集合 + 绑定"**，删掉原来那批开放式正则：金额标签绑定（两侧）、货币数字
   必须等于快照金额、状态名只能取自 `health.rules.ORDER_STATUS` 且与 `status_role` 一致、
   支付结果封闭词表（否定放行）。同时支持 `{{fact:…}}` 片段由系统填值。
3. **协议与表达分离**：`consumer_documents.PROTOCOL_BLOCK`（标记 `<!-- aiops-protocol:2 -->`）
   承载 JSON 字段规则；运营的 SYSTEM 只写业务要求。迁移**只替换协议块**，运营文字一个字不动，
   不需要 `--replace-system`，也不需要"手补协议"。迁移同时写 `answer_fact_contract=slots`。
4. **发布版本契约升级**：`callback_version` 4→5（documents + 片段契约），
   `chat_execution` 接受 1–5；旧发布版本停在 4，行为不变。
5. **在途/历史交付**：`consumer_chat_service.deliverable_binding` 只在"路由推进过版本、凭据/app/
   workflow 未变、路由仍 active"时放行旧 run；换凭据、内容被换、撤回仍 410。`current()` 仍用于
   新会话（严格）。
6. **验收脚本不再写死 pass**：`verify_prompt_lifecycle.py` 在真实 `ChatRouteBindingStore` 上跑
   发布 A→发布 B→撤回，断言"旧 run 仍可交付""撤回后拒"，并把页面级/真实模型项列进
   `NOT_RUN`（不计入通过数量）。

## 三、实跑

| 项 | 结果 |
|---|---|
| `uv run python .../probe.py` | **10 cases; 0 failed**（EXIT=0） |
| `uv run pytest -q tests/`（`-n 4`） | **3677 passed, 9 skipped, 0 failed** |
| `uv run python tools/verification/verify_prompt_lifecycle.py` | **24 项，0 失败**；`not-run.json` 记录 4 项 NOT_RUN |
| `uv run ruff check .` / `format --check .` | 通过 / 通过 |
| `deploy/m2-live-03/images.lock.json` | 随 `src/` 变更同步（192 文件） |

## 四、NOT_RUN（不得记作通过）

- 页面级：在 Dify 编排页真实编辑指定文本框并点预览/发布/恢复（本机无隔离 Dify 实例，
  `127.0.0.1:10018` 不可达）。
- 真实 Java 节点执行与找站工具调用；真实模型的风格观感与稳定性。
- 41 上的在途与历史会话（需合并制品）。
- 16 格样本仍是**合成文本**：证明契约与判据，不证明提示词改变了真实模型的生成。

页面验收脚本与前置条件见 `docs/acceptance/dify-prompt-lifecycle/qa-plan.md` 第五节。

## 五、首轮记录的处置

首轮 `docs/validation/2026-10-30-dify-prompt-lifecycle.md` 与
`docs/开发进度/2026-10-30-dify-prompt-lifecycle.md` **保留原文**（历史），
其中"四类全部拦住""两个契约一致""16 格通过"的结论以本文为准。ADR-0189 已就地修订。
