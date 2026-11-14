Feature: 平台维护业务环境与租户接入登记
  Rule: 公司认证与平台开通登记同时成立才允许业务访问
    Scenario: 同编号租户分属独立环境
      Given 两个独立 Gateway 使用不同环境和数据根
      And 两个环境登记了相同租户编号
      When 各环境用户通过本环境认证查询
      Then 每个请求只访问本环境已登记的业务配置
    Scenario: 已认证但未开通或停用的租户
      Given 用户已通过公司身份验证
      When 租户未登记或环境或租户已停用
      Then 业务请求被拒绝且没有调用业务工具
    Scenario: 运行中撤销登记
      Given 租户已有有效会话
      When 管理员发布停用后的登记版本
      Then 下一次业务请求被拒绝且无需重启 Gateway
    Scenario: 登记不可读或环境不匹配
      Given Gateway 已启用环境登记
      When 登记损坏、缺失或不含本环境
      Then 请求被拒绝而不回退到其他环境
  Rule: 登记修改必须可审计且不得覆盖并发修改
    Scenario: 两个管理员基于同一版本修改
      Given 两个修改声明相同的预期版本
      When 第一个修改提交成功
      Then 第二个修改被拒绝且当前登记保持第一个修改的结果
