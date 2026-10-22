Feature: 租户运维的受控只读工具
  Rule: 服务主体只读取绑定环境和租户的已发布能力
    Scenario: 列表选单并查询状态
      Given 一个已登记且启用的租户服务凭据和发布的两个工具
      When Dify 请求订单列表并查询其中一单
      Then 列表有界且状态属于该订单，并附带来源、时间与证据摘要
    Scenario Outline: 无权限请求不取数
      Given 一个有效的租户服务绑定
      When 请求遇到 <condition>
      Then Gateway 拒绝正文且业务取数次数为零
      Examples:
        | condition |
        | 没有凭据 |
        | 错误租户或环境 |
        | 绑定或凭据被撤销 |
        | agent 被禁用 |
        | 业务地址未登记 |
    Scenario: 发布空白名单
      Given 两个工具已发布且可用
      When 仅编辑草稿关闭工具
      Then 旧发布权限仍有效
      When 发布空白名单
      Then 两个工具的直接 HTTP 调用均被拒绝且不读取业务
    Scenario: 读取期间权限被撤销
      Given 请求已通过首次授权
      When 业务读取期间绑定或发布权限改变
      Then Gateway 在释放结果前拒绝正文
  Rule: 失败可见且存量兼容
    Scenario: 证据写入失败
      Given 一个原本可以完成的请求
      When 证据不能写入
      Then 不返回成功业务正文
    Scenario: 存量工具与地址配置保持
      Given 一个使用历史七工具默认值与标量业务地址的入口
      When 部署新增工具代码但不启用新绑定
      Then 原入口配置和个人授权规则保持
    Scenario: 重复登记提供方
      Given 目标 workspace 已有相同的 aiops-ops 配置
      When 再次运行登记命令
      Then 不重复创建，不切换共享账号 current，不覆盖旧 aiops
