Feature: 管家原生预览与正式 Java 入口
  Rule: 预览复用公司身份和单订单节点授权
    Scenario: 在原生调试框诊断明确订单
      Given 运营通过公司登录且订单在本人可见范围
      When 在 Dify 原生预览发送明确订单与问题
      Then Java 节点执行当前提示词及选中的 CPMS 工具
      And 结果在原预览面板呈现且不改变正式版本
    Scenario: 缺少明确订单或身份
      Given 问题仅为“我的上一个订单”或登录已失效
      When 提交原生预览
      Then 明确提示订单必填或重新登录
      And 不创建业务任务或请求 CPMS
    Scenario: 越权与参数注入
      Given 当前公司账号不能访问某订单
      When 提交该订单或自报租户、站点、工具凭据
      Then 请求被拒绝且不交付订单内容
    Scenario: 终止预览
      Given 预览任务正在执行
      When 点击停止或连接断开
      Then 对应试用任务被取消且不能继续交付
  Rule: 正式路由经发布启用
    Scenario: 持续开放和恢复
      Given Java 节点在线且授权正反例验证通过
      When 发布现有管家应用为 active
      Then 原问答入口以明确订单执行 Java 链路
      And 保留撤回与历史恢复能力
