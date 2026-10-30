Feature: 编排页的提示词写客户端正文，但数字只能来自本轮快照
  产品诉求：在 Dify 编排页改提示词就能调整客户端的说话方式（包括更拟人）。
  此前 `answer` 节点的正文被丢弃（只取 kind/faq_id），所以提示词没有着力点。
  本契约钉住"提示词能写正文"与"不许编数字"这两件事同时成立。

  Rule: 提示词写的正文会被交付

    Scenario: realtime 分支带回 reply
      Given 本轮 status 为 live 且含 live_snapshot
      When answer 节点输出 {"kind":"realtime","faq_id":"","reply":"…"}
      Then 用户看到的正文就是这段 reply
      And 正文里没有 cpms.* 与 status_code=

    Scenario: 没带 reply 的老应用行为不变
      Given answer 节点只输出 {"kind":"realtime","faq_id":""}
      When 用户查询本人订单状态
      Then 正文仍由确定性渲染生成，与改动前逐字相同

  Rule: 数字必须有出处

    Scenario: 漏写快照里的金额
      Given 快照记录支付金额 2.40
      When reply 里没有出现 2.40
      Then 整轮失败，不交付这份正文

    Scenario: 编造一个金额
      Given 快照里没有任何 2.5
      When reply 里写了 2.5 元
      Then 整轮失败，不交付这份正文

    Scenario: 编造数据里没有的时长
      Given 快照里没有任何 47
      When reply 里写了"约 47 分钟"
      Then 整轮失败，不交付这份正文

    Scenario: 小计数不算编造
      Given 快照里有若干条建议
      When reply 里写"共 3 项"
      Then 交付成功

  Rule: 读者要能核对，且看不到内部标识

    Scenario: 缺订单号
      Given 快照的订单号为 2107546575906942977
      When reply 里没有这个订单号
      Then 整轮失败，不交付这份正文

    Scenario: 内部标识泄漏
      When reply 里出现 cpms.orderDetail 或 status_code=1
      Then 整轮失败，不交付这份正文

    Scenario: 空正文或超长正文
      When reply 为空串或超过 2000 字
      Then 整轮失败，不交付这份正文

  Rule: 没有取数依据的地方不许模型写正文

    Scenario: 无实时快照却带了 reply
      Given 本轮 status 不是 live
      When answer 节点仍然输出 reply
      Then 整轮失败，不把这段话当成知识库回答交付

  Rule: 回读仍要拦住改坏的正文

    Scenario: 库里的正文被改成编了数字的版本
      Given 一条已完成的 realtime 答复
      When 库里的正文被改成含快照外数字的内容
      Then 回读被拒绝，不交付改动后的正文

    Scenario: 正常回读
      Given 一条由提示词写成的已完成答复
      When 在有效期内回读
      Then 返回同一段正文，不因"无法逐字重算"而 503
