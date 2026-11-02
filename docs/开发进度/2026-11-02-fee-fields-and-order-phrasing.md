# 费用区字段进答复、「订单」二字不再是门槛

日期：2026-11-02。分支 `fix/fee-fields-and-order-phrasing`，基线 `8bffe408`。

## 一句话

2026-11-02 两张截图：后台费用信息栏有的金额，AI 答复里没有；同一张单改成「这笔单」说
就分析不出来。前者是证据字段表缺项，后者是网关用「有没有『订单』二字」当门槛。

## 已完成

- `operator_tools.json` 订单详情证据字段 26 → 35：补 `isPrePay`、`prePayAmount`、
  `dsElectricFee`、`dsServiceFee`、`launchFee`、`vat`、`vatFee`、`payTime`、`occupyFee`。
  两端（Python 投影、Java `OperatorTools.evidenceFields`）仍从同一声明读。
- `operator_cpms._order_fact_text` 按后台那一栏的顺序渲染这些金额；缺列不出行，值为 `0`
  照写；布尔写「是/否」；付款时间走可读写法。
- `OPERATOR_OUTPUT` 同步说明 `{{fact:order_facts}}` 会出现哪些费用项。
- 新增 `common/order_reference.py`：`names_order()` 判「这条消息在说哪一张订单」。
  正式入口与原生预览桥共用；判据是形状（15 位以上纯数字 / 整条消息是标识符 / 说了
  「订单」），不是词表。
- 控制台桥打包清单与部署测试同步加入这个新模块（服务宿主只装固定文件集）。

## 验证

- 全量 `uv run pytest`：3890 passed / 9 skipped（变更前后同数）。
- 新模块进了 Gateway 镜像上下文，按 `docs/agents/local-gateway-build.md` 真构建镜像并
  `--update-lock`：`files` 198 → 199，`source_sha256` `1f910bbb…` → `a6c2d22e…`；
  `context_digest.py --check` 通过，镜像内导入新模块并跑通判据。
  （首轮我误判这条为「与本次无关的既有失败」——`git stash` 不回收未跟踪文件，那个
  「干净基线」里还带着新模块。订正过程记在验收记录里。）
- `ruff format --check` / `ruff check`：通过。
- 变异验证两条都做过：删掉 `launchFee` 等四个字段 → 费用用例红；把 `names_order` 的
  数字分支改成 `False` → 网关用例与预览桥参数化用例同时红。
- 决策与取舍见 [ADR-0193](../adr/0193-fee-fields-and-order-reference.md)。

## 已知缺口

- 后台「结算状态」列**不是 `getOrderInfoDetail` 返回的字段**，本次没有覆盖。详见下方
  「关于结算状态」。
- **未完成真实订单验收**：真实 CPMS 是否真的返回 `launchFee/vat/vatFee` 等字段、
  单位与币种是否如后台所示，需要在真实账号与订单上复核（见
  `docs/agents/operator-cpms/README.md`）。本轮只有合成响应与本地全链。

## 关于「结算状态没覆盖」是什么意思

后台那张表最后一排有「结算状态」列（截图里是**订单已完成**）。它说的**不是充电订单状态**
（充电中/已结束/异常），而是**这笔钱结没结算给运营商**：0=进行中、1=待结算、2=已结算/结算中、
3=完成（对账单已发放）。这一列在**商城订单**（`cloud-mall-common` 的
`OrderAdminListVo.settlementStatus`）上，我们的充电订单接口取不到它：

| 载体 | 有 `settlementStatus` 吗 |
| --- | --- |
| 商城 `OrderAdminListVo` / `OrderListVo` / `OrderInfo` | 有 |
| 充电订单表 `ChOrderInfo` / `ChOrderInfoModel` | **没有** |
| `getOrderInfoDetail` 的响应 `OrderPageResp` | **没有** |
| 充电占位订单 `ChOccupyOrderInfoController` | 有，但那是**占位费**订单的，不是本单的 |

所以不是我漏加了一个字段，而是这个接口本来就不返回它。要覆盖它得新增一次跨服务查询
（充电订单 → 商城结算单），涉及新的授权面与新的失败模式，不在本次修复范围内。
「结算方式」（`ChOrderInfo.settlementType`，电费单独结算/随服务费结算）也是同样的处境：
PO 上有、两个响应 DTO 上都没有。

**答复里因此不会出现结算状态**，也**不会**用充电订单状态去冒充它。

## 下一步

配套发版：Gateway（新增模块与判据）、Java 节点 jar（证据字段来自声明，需与 Gateway
同版本声明）、控制台桥（新模块进打包清单）。发版后按截图原话复跑两例。
