Feature: 可信业务环境入口
  同一AI入口把请求交给所属环境的独立Gateway，保持原用户身份验证与业务合同。

  Scenario: 两环境使用相同用户与租户编号
    Given 两个独立Gateway登记了不同来源凭据
    When 两个可信代理携带各自来源凭据发送相同用户问题
    Then 每个请求只到达对应Gateway
    And 用户Authorization原样交给该Gateway验证

  Scenario: 伪造环境或未知来源
    Given 已登记两个环境且没有默认环境
    When 请求缺少来源凭据或提供未知、重复的来源凭据
    Then 入口拒绝请求且任何Gateway都未收到它
    When 请求提供环境A凭据并伪造环境B标识头
    Then 只到达环境A且原伪造环境头不被传递

  Scenario: 一个环境不可用
    Given 环境A连接被拒绝且环境B正常
    When 分别向环境A与B提交请求
    Then A返回连接失败而不重试B
    And B仍正常响应

  Scenario: 不安全目录
    When 目录包含重复来源凭据、重复环境、远端HTTP或注入配置字符
    Then 渲染失败且不输出可用配置
