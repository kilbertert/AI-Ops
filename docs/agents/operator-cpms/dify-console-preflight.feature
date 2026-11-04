Feature: Dify 控制台连接必须在启动时满足出站安全规则
  Rule: 控制台密钥只经 HTTPS 或既有本机回环通道发送
    Scenario: 远端明文地址不能成为健康的 Gateway
      Given 已配置控制台密钥且控制台地址是非回环 HTTP
      When 启动 Gateway
      Then 启动失败并指出 AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL
      And 不建立数据库或发送控制台请求
    Scenario: 沿用现有加密隧道恢复原生预览
      Given 既有隧道可读取同一应用草稿和工具目录
      When 控制台地址改为该隧道的本机回环地址并完成标准部署
      And 在原 Dify 页面询问“天河有站点吗？”
      Then 页面显示本轮站点结果并可关联成功的问答运行
      And 正式发布指针与工具授权不因连接修复而改变
