# 65. 工单订单的主人写在 `consumer_id`，`self` 范围要认两列

日期：2026-10-20
状态：已采纳
影响版本：`src/aiops_diagnostics/sources.py`（`MySQLSource._scope_where`、`get_orders`、
`get_fee_template_record`）

## 背景：`ch_order_info.user_id` 不是唯一的「这单是谁的」

`ch_order_info` 有两种订单：

- **普通充电单**：`user_id` 是下单的 C 端用户。
- **工单订单**：建单时写 `user_id = '-1'`（`ChargingServiceImpl` 的工单分支），
  真正的消费者写在 `consumer_id`（列注释「充电用户的id,工单场景」）。公司自己判
  「这单是谁的」时用的就是这两列 —— `ChSendOrderMsgServiceImpl.getUserId`：
  `userId.equals("-1") ? consumerId : userId`。

AI-Ops 的 `self` 范围只下推了 `user_id = <会话 C 端 id>`，于是第二类订单对**它的
主人自己**也是不可见的：用户从自己的订单列表里点进来，网关回「这个订单不属于当前
账号」。这不是越权判定过严，而是认领者列少了一列。

41 生产库实测（2026-10-20，`ch_order_info` 31,319 行）：

| 形态 | 行数 | 说明 |
|---|---|---|
| `user_id='-1'` | 771 | 工单订单 |
| 其中带 `consumer_id` | 347 | 有真实认领者，本票要救回来的 |

`consumer_id` 的 ID 空间是 `qumall_mall.user_info.id`（与 `user_id` 同一空间：
21/21 个不同 `consumer_id` 都能在 `user_info` 里找到）。也就是说这是**同一个人
的另一种记法**，不是另一个主体。

## 决策：在共享的 `self` 渲染里加一列，不给订单表开特例分支

`_scope_where(user_column=..., claim_column=...)` 多一个开关，**只有订单表打开它**：

```sql
(CASE WHEN user_id=%s THEN consumer_id ELSE user_id END=%s)   -- 参数：'-1', 会话 C 端 id
```

写成一个片段是有意的：片段以 ` AND ` 连接、调用方自行拼接，`a OR (b AND c)` 那种
形状会让每一个「按渲染出的 WHERE 选行」的测试镜像都要重新学括号。`CASE` 版本
仍然是一个片段、两个参数，语义相同。

三条边界，都是刻意的：

1. **只认哨兵值 `'-1'`**。空白与 NULL 是**没有人**认领的行（渠道/OCPI 单据，
   实测 1,197 行里 822 行如此），不是「本人」。把它们当成本人等于放开一整类单据。
2. **只有 `ch_order_info` 有 `consumer_id`**。占位单（`ch_occupy_order_info`）、
   设备表都没有这一列，所以它必须是渲染开关而不是全局行为 —— 否则那些查询在真库上
   直接 `Unknown column`。
3. **`get_fee_template_record` 也打开它**。模板表自己没有认领者列，那里的谓词是拿去
   查 `ch_order_info` 的存在性；不打开就会「订单查得到、计费快照查不到」，而计费快照
   正是诊断要用的一半证据。

## 备选方案（为什么没有选它们）

- **备选一：不放 `CASE`，改成 `user_id=? OR consumer_id=?`。** 被否，理由是它会**放宽**
  而不是补全：「这单是谁的」在工单单里只有 `consumer_id` 一个答案，`user_id` 是哨兵；
  写成无条件的 `OR` 会让任何 `consumer_id` 为空的 `-1` 行也匹配 `user_id='-1'`，
  于是「查哨兵就等于查所有无人认领的单」。锚定哨兵值是这条规则的实质。
- **备选二：把规则放到每个调用方（`ScopedOrderAuthorizer`、工具层、`/diag/*`）各写一次。**
  被否，理由是这正是本仓此前六份实现漂移的形态；共享的 `_scope_where` 是唯一收敛点。
- **备选三：改成租户级可见（同租户都能查）。** 被否，是越权：同租户的其他用户会因
  此看到不属于自己的单。
- **备选四：把 `enterprise_user_id` 也并进来。** 推迟，不是否定：它是「车队/企业会员」
  的 id，`wallet_type=1` 的单由企业钱包付款，把「车队的单」算成「某个司机的单」是另一条
  业务口径，需要产品先裁定。实测 272 行 `enterprise_user_id<>user_id`；按现在的口径这些
  单对下单人仍可见（`user_id` 是他），只是对车队不可见。

## 验收

- `tests/test_mysql_scope.py`：`self` 渲染出 `CASE` 那段、参数是 `['-1', C_USER]`；
  没有该列的表仍只渲染 `user_id`。删掉开关，这两条转红（已做过变异）。
- `tests/test_operator_order_authorization.py`：`ScopedOrderAuthorizer` 对
  「本人是消费者的工单单」为真，对「别人是消费者的工单单」与「没人认领的工单单」为假。
- 生产验证边界：本票只改 SQL 谓词，不改任何路由形状；**它不能证明真库上的索引使用
  情况**（`user_id`/`consumer_id` 是否走索引需要在 41 上 `EXPLAIN` 一次）。

### 已做的生产只读验证（2026-10-20，41）

同一驱动脚本、同一批真实会话与真实行，只换「线上那份 / 修后副本」两个变体：

| 用例 | 线上（部署中） | 修后副本 |
|---|---|---|
| 本问题订单（普通单）主人 | True | True（对照：普通单零回归） |
| 同租户他人 | False | False |
| 工单订单 `2064315298158161921` 的 `consumer_id` | **False** | **True** |
| 同一工单订单，非消费者 | False | False |
| 无人认领的 `user_id='-1'` 行（对 `-1`） | False | False |

`EXPLAIN` 对照：改动前后都是 `type=ALL`，`possible_keys=None` —— 这不是本票引入的退化，
`ch_order_info` 只有 `PRIMARY(id)` 与 `device_id` 两个索引，`order_no` 本来就不走索引。
把 `order_no` 加索引是另一个决定（写放大 + 公司库的变更流程），不在本票内。
