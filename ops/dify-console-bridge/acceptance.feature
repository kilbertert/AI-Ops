Feature: Dify 控制台发布桥的授权与可复现部署
  发布由 AI-Ops 的发布门执行，浏览器不得持有机器发布密钥。

  Rule: 只有固定工作区中被允许的角色能够发布已登记应用
    Scenario: 管理员发布当前应用
      Given 浏览器携带有效 Dify 会话与相符的 CSRF
      And 用户在桥固定的工作区中具有允许的发布角色
      And 应用在 Dify 和 AI-Ops 登记表中均可解析
      When 用户点击发布到 AI-Ops
      Then 发布门返回明确成功动作与正整数版本号
      And 浏览器收到发布成功

    Scenario: 其他工作区的管理角色不能借用目标工作区的查看权限
      Given 用户在其他工作区是管理员而在目标工作区只是普通成员
      When 用户在工作区切换期间请求发布
      Then 桥拒绝不匹配的工作区摘要
      And 没有调用网关发布门

    Scenario: 查看应用不等于发布权限
      Given 用户能查看应用但不具有允许的发布角色
      When 用户请求发布
      Then 桥拒绝请求且没有调用网关发布门

  Rule: 凭据与响应必须在固定边界内
    Scenario: CSRF 或同源上下文缺失
      Given 请求缺少或篡改 CSRF 或来自其他 origin
      When 请求到达桥的 HTTP 入口
      Then 桥返回拒绝并不调用上游

    Scenario: 上游要求重定向或返回不明确成功结果
      Given Dify 或网关返回重定向或不符合约定的响应
      When 桥处理发布
      Then 不跟随重定向
      And 不报告发布成功
      And 凭据不发送给重定向目标

  Rule: 运行实例必须可由合并制品恢复
    Scenario: 按合并版本重新部署
      Given 产物来自保护主分支的合并修订
      And 桥工作区 ID 与网关 DSL 导出工作区 ID 相同
      When 启动服务并通过宿主反代访问控制台
      Then 部署模块摘要与产物一致
      And 注入脚本可访问
      And 发布桥与 Dify 内部入口只绑定回环

    Scenario: 保存草稿不会上线
      Given 线上已有已发布版本
      When 运营只保存 Dify 草稿而不点击 AI-Ops 发布按钮
      Then 线上仍使用原已发布版本

    Scenario: 桥部署中途失败
      Given 原桥、页面脚本和转发配置已完整备份
      When 新制品安装后 nginx reload 失败
      Then 原桥代码、页面脚本和转发配置全部恢复
      And 部署命令报告失败而不是成功
