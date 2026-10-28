Feature: 测试同事通过公网 OpenAPI 查看用户接口
  Rule: 公开文档不改变业务鉴权
    Scenario: 未登录访问规范与查阅页
      Given Gateway 已启动
      When 访问 GET /v1/openapi.json 和 GET /v1/docs
      Then 返回有效的 OpenAPI 3.1 JSON 与中文接口查阅页
      And 规范只含测试文档列出的 23 个用户接口
      And 无凭据请求业务接口仍被拒绝

    Scenario: 导入规范后区分调用身份
      Given 测试人员将规范导入接口测试工具
      When 查看 C 端与管家端请求配置
      Then C 端使用 third-session，管家端使用个人 Bearer
      And 参数保留实现的长度、格式与必填约束
      And QA、旧诊断和分析分别返回自己的任务 ID
      And 媒体使用签名 URL，不要求额外的个人凭据

    Scenario: 内部接口没有被自动公开
      Given Gateway 还注册了管理接口和内部回调
      When 下载公开规范
      Then 其中没有管理接口、内部回调、服务密钥字段或部署配置
