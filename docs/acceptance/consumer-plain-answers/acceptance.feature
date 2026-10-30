Feature: C端订单状态与账单答复先说结论、再用白话说清每一项
  2026-10-30 反馈：答复"内容都对，但商务看不懂"。查到的形状是**字段罗列**：没有一句
  「这单怎么样」，术语（`支付金额`／`未提供`／`cpms.orderDetail`／`status_code`）原样
  摆给读者。本契约钉住可观测的结果，实现取舍见 ADR-0181。

  Rule: 正文第一段就是结论

    Scenario: 账单答复第一段给出金额结论
      Given 本人可见的一张订单，账务记录里订单金额 27.36、实付 0.00、无退款记录
      When 用户查询该单的账单
      Then 正文第一段同时出现合计金额与实付金额
      And 第一段出现"没有退款"

    Scenario: 状态答复第一段给出状态结论
      Given 本人可见的一张订单，业务状态为"充电结束"
      When 用户查询该单的状态
      Then 正文第一段出现"充电结束"
      And 第一段不是字段名开头

    Scenario: 上游没给的字段不编造
      Given 账务记录里已退金额为空
      When 用户查询该单的账单
      Then 正文说明这一项没有记录
      And 正文不出现任何未经上游提供的金额

  Rule: 术语要用读者能核对的说法

    Scenario: 支付金额不冒充"已扣款"
      Given 账务记录里实付金额为 0.00
      When 用户查询该单的账单
      Then 正文说明这是账务记录里的数、不等于银行已扣款
      And 不出现"已经扣款""已支付成功"这类断言

    Scenario: 内部接口标识不出现在正文
      Given 账务或状态记录来源标识为 `cpms.orderDetail`／`cpms.chargingInfo`
      When 用户查询该单
      Then 正文出现"订单账务记录"或"充电状态记录"这样的人话来源
      And 正文不出现 `cpms.` 前缀的标识
      And 正文不出现 `status_code=`

    Scenario: 空字段说明是上游没有，不是我们没查
      Given 账务记录里币种字段未提供
      When 用户查询该单的账单
      Then 正文说明币种需与原订单页面核对

  Rule: 十一语种同形，占位符集合不变

    Scenario Outline: 任一支持语言都先给结论
      Given 页面语言为<language>
      When 用户查询该单的状态与账单
      Then 两种答复的第一段都包含该语言的结论句
      And 订单号与可核对时间仍出现在正文
      And 不出现微秒 ISO 串
      Examples:
        | language |
        | zh       |
        | zh-Hant  |
        | de       |
        | fr       |
        | es       |
        | pt       |
        | vi       |
        | mn       |
        | th       |
        | km       |

  Rule: 只换措辞，不扩权限、不动免责语义

    Scenario: 免责语义逐字保留
      Given 账单答复渲染完成
      When 读取正文
      Then "支付金额不证明已扣款、退款记录不证明银行到账"的含义仍在正文中
      And 正文声明本客服未发起任何付款或退款

    Scenario: 语气改变不放宽答复条件
      Given 调用者没有该单的可见范围
      When 请求该单的状态或账单
      Then 仍按原判据拒绝，不返回任何订单字段
