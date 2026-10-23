Feature: 客户端通过既有聊天入口查询本人订单状态
  Rule: 部署显式启用且查询按本人范围执行
    Scenario: 查询本人最新一单
      Given consumer 本人会话和同租户不同用户及工单订单
      When 用户在现有助手入口请求本人最新订单状态
      Then SQL 在排序前按租户和本人归属过滤
      And 只查询选中一单的 CPMS 当前状态并返回可渲染的回答
    Scenario: 查询指定订单
      Given consumer 本人会话
      When 用户提供可见订单号并明确查询当前状态
      Then 返回该单状态码、业务含义、来源和观察时间
      And 不启动模型或故障诊断
    Scenario Outline: 不可信或不可用的读取
      Given 已启用状态查询
      When 请求遇到 <condition>
      Then 不返回成功业务正文
      Examples:
        | condition |
        | 缺身份 |
        | 非本人或代查范围 |
        | 他人订单或无本人订单 |
        | 授权源失效 |
        | CPMS 失败或返回其他订单 |
        | 读取后订单不再可见 |
        | 证据写入失败 |
    Scenario: 原入口保持兼容
      Given 未启用新能力或用户询问故障/扣费原因
      When 用户提交原有请求
      Then 原有诊断、知识问答与快捷动作路由保持
  Rule: 证据与当前观察诚实呈现
    Scenario: 未知状态和会话交错
      Given 两个独立用户的查询与未知状态码
      When 查询完成并读取既有助手结果
      Then 未知状态不编造且用户之间不串单
      And 回答与持久化快照摘要一致并带 no-store
