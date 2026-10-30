# C端答复先说结论：从"字段罗列"改成"下结论 + 人话注释"

分支`feat/consumer-plain-answers`，基线`5de086c3`。
设计取舍见 [ADR-0182](../adr/0182-consumer-answers-lead-with-the-conclusion.md)，
契约见 [acceptance.feature](../acceptance/consumer-plain-answers/acceptance.feature)。

## 起点：上一片解决了排版，没解决内容

[ADR-0180](../adr/0180-answers-are-plain-text-laid-out.md) 上线后，C 端账单答复变成逐行
一项，但仍然回答不了读者真正问的两件事：**这单到底多少钱**、**`0.00` 是没付还是没扣**。
`未提供` 分不清「上游没这个字段」与「我们没查」，`cpms.chargingInfo`、`status_code=1`
则完全没有信息量。

## 已完成内容

- 新增 `consumer/consumer_plain.py`（叶子模块，只依赖 `consumer_time`）：十一语种的
  状态/账单词表与拼装。三条渲染路（`consumer_order_chat`、`consumer_billing`、
  `consumer_language.live_answer`）共用同一套说法。
- **结论句先行**，只由真实存在的值拼：账单是「这单的账单合计 27.36，系统记录的支付金额是
  0.00，没有退款记录。」，状态是「这单当前的状态是「充电结束」。」。缺哪项就不写哪项，
  不补数字。
- **术语就地解释**：支付那一行写明「不等于银行已经扣款」，退款那一行写明「有这条记录也不
  代表银行已经到账」；缺值写成「上游未提供，需到原订单页面核对」。
- **内部标识退出正文**：来源写人话（`cpms.chargingInfo` → 「充电状态记录」、
  `cpms.orderDetail` → 「订单账务记录」），`status_code` 占位符从**所有语种一起下线**，
  原始码只留在存储的快照里。
- `consumer_language.json` 九个语种的 `status`／`billing` 模板重写，新增三个来源标签键；
  占位符集合逐语种保持一致（`test_fixed_copy_preserves_placeholders_*` 会拦）。
- README 模块表补上 `consumer.consumer_plain.py`（漏了 `test_readme_architecture` 会红）。
- 测试：新增 `tests/consumer/test_consumer_plain_answers.py` 19 条；改写六处原本断言
  「内部标识必须出现在正文里」的用例，改为断言人话来源与「标识不出现」。

## 一个刻意的非改动

`chat_execution.answer_matches_snapshot` 用当前代码重算正文与库里存的比对，所以改措辞
会让部署瞬间正在回读的会话落到 `qa_answer_mismatch`（节点路线钉终态，Dify 路线 503）。
窗口受 `RUN_TTL_SECONDS = 180` 限制，是分钟级而非历史数据。

**本次接受这个窗口，不去改成"读不到就重渲染"**：那等于让篡改检测悄悄失效，而这正是
`final_release` 拒绝交付时要拦的东西。真要两头都满足，需要给渲染加版本号并按版本放行，
那是另一片的事，已写进 ADR-0182「代价」一节。

## 验证

| 项 | 结果 |
| --- | --- |
| `uv run ruff check .` / `ruff format --check .` | 通过（1120 files already formatted） |
| 全量 `uv run pytest -n 8` | **3482 passed / 9 skipped**，95s |
| 新增用例 | 19 条，覆盖结论先行、缺值说明、免责语义、标识不出现、十一语种同形、词表同构 |
| 变异验证 | 结论句换成固定句 → 2 条红；删掉「不等于银行已经扣款」→ 1 条红；恢复后全绿 |
| 内容锁 | 按合入后的树用 `build-image.sh --update-lock` 重建：`source_sha256=b44ef1d5…` |

## 已知缺口

- **未完成业务验收**：真实账号、原 H5／小程序上的实际观感（换行、`·` 项目符号、结论句是否
  真的在第一屏）只能人眼确认，本片没有做。
- 部署瞬间的回读窗口（见上）未消除，只是被限定在 180 秒内。
- `en` 仍走代码词表、其余八种走 JSON 模板：一种语言要改两处，这是既有分工，未在本片搬掉。
- 九种非中文译文由本轮补写，未经过母语者复核；句子结构沿用各语言包里已有的句式。

## 下一步

1. 推分支、开 PR、跑确定性 CI。
2. 合并后按部署流程上线；上线后用一次正常登录在真机上核对两种答复的实际渲染。
3. 若部署窗口影响被观测到，再按 ADR-0182「代价」里记的方向做渲染版本号。
