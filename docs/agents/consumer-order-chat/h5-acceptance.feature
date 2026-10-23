Feature: consumer H5 现有聊天请求到达网关
  Rule: 只转发现有查询面并覆盖可信身份
    Scenario: 查询提交与回读
      Given 已部署的 H5 在本站调用助手查询
      When 用户提交问题并读取 QA 结果
      Then 请求抵达既有网关路径并保留 JSON 状态码和正文
      And 来源密钥被清空且入口固定 consumer
    Scenario: 不转发管理路径
      Given H5 vhost 原有静态路由
      When 请求管理、诊断或不合法 QA 路径
      Then 本片不会将其转发到网关
    Scenario: 方法必须对得上
      Given 同一条路径上读与写是两个不同的端点
      When 用 GET 打 /v1/assistant/questions 或用 POST 打 /v1/shortcuts
      Then 两者都不进代理，返回 405 或静态回退
    Scenario: 失败不重试
      Given 上游拒绝或不可用
      When H5 发出一次查询
      Then 状态如实返回且不缓存、不自动重试业务请求
