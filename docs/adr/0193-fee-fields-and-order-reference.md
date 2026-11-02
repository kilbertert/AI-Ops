# 193. 费用区字段进证据与答复；「订单」二字不是订单门槛

日期：2026-11-02
状态：已采纳，随配套节点、Gateway、控制台桥交付

## 背景

2026-11-02 的两条真实反馈，同一张截图对：

1. 后台「新能源车充电订单」的费用信息栏能看到是否预付费、预付费、电费、服务费、电损电费、
   电损服务费、**启动费用**、充电总费用、**优惠金额**、**增值税比例**、**增值税费用**、
   实付金额、结算状态、付款时间；而 AI 的答复只有电费与服务费。后台看得到的钱，
   在答复里看不见。
2. 同一张单，原话「帮我分析这笔单的电费2116050516255272961」送进管家，答复是
   「本轮我没有订单查询工具与历史订单上下文……麻烦你先选择或提供具体订单」；
   只把「这笔单」改成「这笔订单」就能分析。单号明明在句子里。

两条是**两个独立缺陷**，只修一条不足以让这两张截图对得上。

## 决策

### 一、费用字段：先进声明，再进渲染

- `operator_tools.json` 的 `cpms_order_detail.evidence_fields` 从 26 项补到 35 项：
  `isPrePay`、`prePayAmount`、`dsElectricFee`、`dsServiceFee`、`launchFee`、`vat`、`vatFee`、
  `payTime`、`occupyFee`。声明是唯一来源，Python 投影（`operator_cpms.DETAIL_FIELDS`）与
  Java 节点（`CpmsReadTool` 经 `OperatorTools.evidenceFields`）都从它读，两端仍逐项相同。
- `_order_fact_text` 按后台那一栏的顺序渲染，**只列上游给了值的项**：缺字段就是
  「后台没有这一列」，不补零、不补「未提供」。上游真的给了 `0` 就写 `0`。
- 布尔写成人话（`是/否`，`Yes/No`），不把 `True` 印给用户；`payTime` 走既有的
  `_readable_time`，不把 CPMS 的 ISO 串摆进正文，也不补一个 CPMS 没给的 UTC 后缀。
- 标签与公司自己的一致：`launchFee` 用 `orderDetail.startupFee`（启动费用 / Startup Fee）、
  `vat` 用 `orderDetail.vat.label`（增值税率 / VAT）、`vatFee` 用 `orderDetail.taxFee`
  （税费 / Tax Fee）、`marketAmount` 用 `orderDetail.discountReductionAmount`
  （优惠减免金额 / Discount Amount）、`occupyFee` 用 `orderDetail.placeholderFee`
  （占位费 / Placeholder Fee）。后台表头与 App 订单详情页本来就是这两套词，
  我们不自创第三套。`dsElectricFee/dsServiceFee`（电损电费/服务费）两套词里都没有，
  取字段注释的直译。
- `OPERATOR_OUTPUT` 同步声明这些字段，让模型知道 `{{fact:order_facts}}` 里会出现什么、
  不必也不该自己复述或算差额。

**没有加入的列**：后台那张表最后的「结算状态」列（截图显示**订单已完成**）说的是
**这笔钱结没结算给运营商** —— `0=进行中, 1=待结算, 2=已结算/结算中, 3=完成（对账单已发放）`，
见 `cloud-mall-common` 的 `OrderAdminListVo.settlementStatus`。它属于**商城订单**，
不属于充电订单：

| 载体 | `settlementStatus` |
| --- | --- |
| 商城 `OrderAdminListVo` / `OrderListVo` / `OrderInfo` | 有 |
| 充电表 `ChOrderInfo` / `ChOrderInfoModel` | 无 |
| `getOrderInfoDetail` 响应 `OrderPageResp` | 无 |

同样的处境还有「结算方式」`ChOrderInfo.settlementType`（电费单独结算/随服务费结算）：
PO 上有、两个响应 DTO 上都没有。所以这不是漏挑字段 —— 把它写进 `evidence_fields`
只会让每次运行都少一个字段，不会让它出现。要覆盖得新增一次跨服务查询（充电订单 →
商城结算单），带新的授权面与新的失败模式，不在本次范围。**答复里不会出现结算状态，
也不会用充电订单状态去冒充它。** 这是**已知缺口**，记在这里而不是假装覆盖。

### 二、订单门槛：判据是形状，不是有没有那两个字

新增 `common/order_reference.names_order(text)`，正式管家入口（`gateway_api`）与
原生预览桥（`dify_console_publish._native_order`）**共用这一份**。成立条件三条：
说了「订单/訂單/order」；整条消息就是一个带数字的标识符；句中有一段 15 位以上的纯数字。

- 门槛本来挡的是**别的东西里的数字**：站点名 `TEST2026-10-29`、时刻 `03:00:09`、
  手机号。它们要么被 `-`/`:` 切开，要么短于 15 位；系统订单号是 19 位雪花 id。
- 「这笔单」「这单」「这一笔」因此不需要另立词表 —— 词表是第二份事实，会与真话术脱节。
- `order` 的边界用 `(?<![A-Za-z])order(?![A-Za-z])` 而不是 `\border\b`：Python 的正则里
  中文也算 `\w`，`电费ORDER-1001` 里的 `\border\b` **匹配不到**，而它正是要认的那一句。
  这条在写测试时才暴露 —— 合成单号 `ORDER-1001` 紧贴中文。
- **门槛与提取必须看同一组候选。** 这是评审抓出来的：门槛只问「句中有没有 15 位以上
  数字段」，而提取器从左到右取**第一个含数字的 token**，于是
  「分析站点 TEST2026-10-29 这笔单2116…」被放行之后，真正去查的是那个**日期**。
  候选集因此定成一个函数 `order_reference.digit_runs()`，网关与预览桥都从它取；
  纯数字段唯一时它就是这一轮的单号。反面情形（多段数字）不擅自挑一个，仍按「说不清」处理。
- **中英的显式写法分开匹配。** 中文可以紧贴单号（`订单2116…` 是原生写法），英文必须有
  分隔（`order 123` / `order no: 123`）。写在同一组里时，`ORDER-1001` 里的 `ORDER` 会被
  当成英文单词再截出一个 `-1001`，同一张单变成两个候选、整条请求被拒（评审实测）。
  只给 `order` 加左边界不够 —— 「订单号:」的中文前缀紧贴 `order` 之前，那样会连它一起
  匹配不到。

## 被否的替代方案

- **只改提示词，告诉模型「这笔单」也是订单。** 门槛在网关 Python 里，模型看不到这条
  消息：它进的是**无订单回合**，那一轮的工具清单里根本没有订单工具（ADR-0182/0184）。
- **给「这笔单/这单/这一笔」加词表。** 换个说法又失效，且维护成本落在运营身上。
- **把门槛整个删掉。** 站点名与时刻会被当成订单号，`OPERATOR_TARGET_MISSING` 或
  签发失败；`test_site_name_with_date_is_not_an_order` 与
  `帮我查询充电站 TEST2026-10-29 03:00:09` 两例守着这条线。
- **只补证据字段、不改渲染。** 模型拿得到金额，但用户看的正文里仍然只有电费和服务费 ——
  第一张截图的比较对象就是正文。

### 三、输出契约的措辞本身会改成功率（2026-11-02 线上实测）

上面两条部署之后，同一句原话连跑 3 次，**2 次整轮失败**，网关日志
`reason=unsupported_fact`。这不是新缺陷，是第一条改动**引出**的：

`reply` 的非事实段行由 `check_notice` 判「不许出现任何数字」（防模型凭空写金额）。
而这次把费用字段名**枚举**进了「系统会在这里显示…」那句话，紧接着又写「不要复述这些值」——
两句话语义打架，模型把枚举当成可谈的内容，开场就带上金额或百分比，于是整轮被拒。

修法不是放宽 `check_notice`（那道墙挡的是真问题），而是**把禁令写死并提高优先级**：
其它行一个字都不要带数字（金额、百分比、条数、时间都算），并明说不要提具体是哪几项费用。

这条留在这里的理由：**枚举字段名是必要的信息，但它与"禁止复述"相邻时会诱发违规**。
以后往这段契约里加内容，要同时看它会不会被读成"这些是可以说的"。

被否的替代：放宽 `check_notice` 允许事实段外出现与证据相同的数字 —— 那等于让模型
自己判断"抄得对不对"，而这正是该检查存在的理由（ADR-0190：系统写事实、模型写语气）。

### 三之二、那次「把禁令写死」的改法，生产实测更差，已回滚（2026-11-02 同日）

上面那段改法（#1307）合并进 main 后，先按「不就是更新版本的问题」把它发上了生产：
用 `business_prompt(v2) + 当前 OPERATOR_OUTPUT` 重发节点清单为 v3，路由指针推进到
v3 / revision 14。产物与**受支持的发布链输出逐字节相同**，所以这不是发错了文本。

结果：**v3 上连跑 8 次，0 次成功**；同期旧契约（rev 12）是 13 成功 / 30 次。

```
(12, 'old') {'succeeded': 13, 'failed': 15}
(14, 'new') {'failed': 8}
```

按旧契约 43% 的成功率，连败 8 次的概率约 1.1%；而且失败原因的**种类**变了 ——
出现了旧契约里没有的 `reason=shape` 与 `OPERATOR_EVIDENCE_MISSING`。

**判断**：把「一个问题一种失败方式」改成「禁止一切数字 + 禁止提费用项」之后，模型不是
更守规矩，而是**换了一种失败方式**（结构不完整、缺证据）。这条路不通，已回滚到 v2 /
revision 15。

**留下的规则**：`OPERATOR_OUTPUT` 不是普通措辞，是**输出契约**。改它要按改协议对待 ——
先有可复现的失败证据，再小步改，并且**必须用真实入口对比改前改后的同一句话**，
不能因为「文本发上去了」就认为改好了。

**还没做到的**：失败 run **不保存模型原文**（`snapshot_json.answer` 恒为 `None`，实测），
所以至今只能凭 `reason=` 判断，而 `unsupported_fact` 一条码覆盖好几种成因。
本轮已在拒绝点补上「哪条规则、第几行、该行长度」的取证（不记原文、不记金额），
下次线上失败才第一次能看到真实成因。

## 验证

- `tests/operations/test_operator_reply_facts.py::test_every_backend_fee_field_reaches_the_answer`
  走**投影 + 渲染**全链（不是手写证据）。**变异验证**：从 `evidence_fields` 删掉
  `launchFee/marketAmount/vat/vatFee`，该用例失败。
- 同文件 `test_fee_lines_are_omitted_when_the_backend_has_no_such_column`：缺列不补行，
  值为 0 照写 0。
- `tests/operations/test_operator_cpms.py::test_order_written_without_the_word_order_still_reaches_the_order_turn`
  用真实入口（`POST /v1/assistant/questions`）断言这一句**签发了订单诊断并真的读了那张单**。
- `tests/common/test_order_reference.py`：正例反例两头覆盖；**变异验证**证毕 —— 把
  `names_order` 的纯数字分支改成 `False`，网关用例与预览桥参数化用例同时失败。
- `tests/operations/test_operator_native_preview.py` 增四条原话变体。
- 全量 `uv run pytest`：3890 passed / 9 skipped（变更前后同为这个数）。
- 新增模块改变了 Gateway 镜像构造上下文摘要（198 → 199 个文件，`1f910bbb…` → `a6c2d22e…`），
  按 `docs/agents/local-gateway-build.md` 真构建镜像并 `--update-lock`，`context_digest.py
  --check` 通过；镜像内导入 `order_reference` 并跑通判据。

## 缺口与后续

- 后台「结算状态」列与 `settlementType` 仍未覆盖（见上）。
- 纯电价分时段字段（`tip/peak/flat/valley*Fee`）与 `platformAmount` 在 41 上仍全为 NULL，
  维持 ADR-0171 的判断不加。
- **本地通过不证明真实订单上的行为**：真实 CPMS 响应是否真的带
  `launchFee/vat/vatFee` 等字段、单位与币种是否如后台所示，仍需按
  `docs/agents/operator-cpms/README.md` 在真实账号与订单上复核。本次未做该验收。
