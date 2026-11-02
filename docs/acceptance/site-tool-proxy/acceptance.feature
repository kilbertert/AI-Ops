Feature: 节点通过授权代理取得站点并交付可验证证据
  Scenario: 管家和消费者均从真实代理获取站点
    Given 合成身份和 CPMS 上游，真实 Gateway 与 Java 工具
    When 节点分别以空参数和用户提到的关键词调用站点工具
    Then Gateway 接收 Java 生成的证据，站点身份和分页字段完整
    And 模型证据中没有租户身份字段或额外个人信息
  Scenario: 权限与生命周期仍然约束取数
    Given 同一次运行及其确定的授权范围
    When 上游返回跨租户站点、代理收到错误目标或运行被撤销
    Then 不交付越权证据
  Scenario: 回调只开放声明路径
    Given 按部署模板启动的真实 TLS 回调代理
    When 节点请求站点授权、取数及证据端点
    Then 请求到达 Gateway，契约头和参数保持原值
    And 非声明路径与错误方法被拒绝
