# 回答正文可读排版：解卡 #1175 并补登记与内容锁

分支`fix/readable-answer-output`（PR #1175），基线`bf6085eb`。
本切片只做**一句话**：把已经写好、但合不进去的那一版可读排版推到能合并的状态。
排版本体的设计与取舍见 [ADR-0180](../adr/0180-answers-are-plain-text-laid-out.md)。

## 起点：反馈与既有工作

产品反馈「管家端与客户端的回复完全不适合人看」后，`ab4c9503` 已经实现过一版：
管家诊断报告按【订单事实】【可能原因】【建议下一步】【证据缺口】【数据来源】分节、
列表带编号、来源写「订单详情／充电监测样本」而非 `cpms.orderDetail`、
时间渲染为 `2026-10-29 12:34:45 UTC`。客户端状态与账单逐行一项，免责说明单独成段。

那一版卡住的两个原因都不是设计问题，而是交付面的：

1. **ADR 撞号**：它占用 `0174`，而 #1197 已先合并 `0174-capability-is-the-run-type.md`。
   `Workflow policy` 的 `adr.check.mjs` 因此红，PR 显示为「CI 卡住」。
2. **合并后内容锁失配**：合入新 main 后树的构造摘要变成 `90e54f83…`，
   锁里记的还是 main 那次构建的 `e3ff5c96…`，
   `test_gateway_context_digest_is_reproducible_and_matches_the_lock` 红。

## 本切片做了什么

- 合入`origin/main`（`bf6085eb`），两处冲突都是版本噪声：
  README 的模块职责表按 main 的归包结果取平（`gateway.*`、`common.http_auth`、
  `operator.operator_conversation`），再把本分支新增的 `consumer.consumer_time.py`
  补回「C端业务」一行 —— 少了它 `test_readme_architecture` 的「每个模块都要被点名」会红。
- ADR 换号 `0174` → **`0179`**，并在记录里写明换号原因。
- 用 `deploy/m2-live-03/build-image.sh --update-lock` **真实重建**镜像并按新树写回内容锁，
  不手工拼 `image_id`/`source_sha256`：那会造出一对不复现的身份，preflight 反而失败。
  重建产物 `aiops-m2-live-03-gateway:readable-output-merged`，
  `image_id`=`sha256:61031572485d…`，`source_sha256`=`90e54f83…`，
  基础镜像 `python@sha256:70729b46…`（与锁里原有 base_digest 一致）。

## 验证

| 项 | 结果 |
| --- | --- |
| `uv run ruff check .` / `ruff format --check .` | 通过（1107 files already formatted） |
| `tests/consumer` + `tests/operations` + `tests/tooling` + `test_readme_architecture.py` | 965 passed / 2 skipped（两条既有环境跳过，与本次无关） |
| `node .sandcastle/adr.check.mjs --base origin/main` | ok，169 records |
| `git diff --check` | 通过 |
| 内容锁一致性 | `context_digest.py --check` 与锁一致，工具链专项 18 passed |

## 已知缺口

- **只改了排版，没改内容。** 客户端账单仍是「订单金额：…；支付金额字段：…」这种字段罗列，
  没有一句「这单总共多少钱、有没有退款」的结论；术语（`支付金额字段`）也没有人话注释。
  产品这次的原话是「要下结论，然后给通俗易懂的注释，最起码商务能看得懂」——
  **这一条本切片没有解决**，需要下一个切片在 `consumer_billing.py` / `consumer_order_chat.py` /
  `consumer_language.json` 上做。
- 知识库与零阶自由问答的正文由模型生成、走 Dify 链路，本切片同样未覆盖（ADR-0180 已如实写明）。
- 未部署、未做真实环境验收；真实账号与原 H5 回读仍待验。
- 文案即回读一致性键：`chat_execution.answer_matches_snapshot` 用**当前代码重算**正文与
  库里存的那一份比对。改措辞会让部署瞬间正在回读的会话落到
  `qa_answer_mismatch`（`run` 被 `refuse` 钉成终态）。窗口受 `RUN_TTL_SECONDS = 180`
  限制，影响是分钟级而非历史数据，但部署时要知道这一条。

## 下一步

1. 推分支、更新 PR #1175 正文、跑确定性 CI。
2. 合并后按部署流程上线；`aiops-admin` 侧无配置变更。
3. 下一个切片做**内容层**：结论先行 + 术语通俗注释 + 八语言模板同步。
4. 该内容层同样要更新 `acceptance.feature` 与 `qa-plan.md` 的可观测断言。
