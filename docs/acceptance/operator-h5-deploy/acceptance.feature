Feature: 管家原 H5 的助手交付
  Rule: 助手请求保持公司个人身份与最小路由范围
    Scenario: 已登录管家发起单订单任务
      Given 干净合并的公司 H5 制品已安装且当前用户正常登录
      When 用户从首页进入助手并选择可见订单
      Then 页面显示本次 Java QA 的真实结果
      And 同域代理保留个人 Bearer 并固定 operator 入口
    Scenario: 未声明的方法和管理接口
      Given H5 只声明问答与快捷入口
      When 请求管理接口或向快捷列表发送 POST
      Then 请求不会到达业务上游
    Scenario: 可恢复的制品切换
      Given 当前站点有可用旧版本
      When 新制品摘要不符或 Nginx 检查失败
      Then 旧站点配置保持可用
      And 旧静态文件不被覆盖或删除
