Feature: 旧 Python 执行器退役后的业务交付
  Rule: 用户只通过已发布的业务路线执行
    Scenario: C端检测本人订单
      Given 用户正常登录且选定本人可见订单，C端工作流已发布
      When 用户在原助手入口提交订单检测快捷动作
      Then 返回 QA 结果并显示本单当前状态与来源
      And 本轮状态与独立 CPMS 读取一致
      And 不启动 Python 诊断线程
    Scenario: 管家检测授权订单
      Given 管家正常登录且订单属于当前公司授权范围
      When 管家提交明确订单
      Then Java 节点完成本次 QA，证据来自本轮授权 CPMS 工具
      And 结果能由同一用户在原入口读取
    Scenario: 不可见订单和结果
      Given 用户不能访问另一用户的订单或 QA
      When 用户请求该订单或结果
      Then 请求被拒绝且不返回业务正文
    Scenario: Dify 草稿试用
      Given 已登记应用与有效发起人身份
      When 发起该应用草稿试用
      Then 使用对应 Dify 或 Java 草稿路线
      And 正式发布指针保持不变
  Rule: 退役能力不再可用
    Scenario: 旧诊断与设备入口
      Given 升级后的 Gateway 正常运行
      When 请求旧标准诊断、设备注册或旧运行接口
      Then 返回 404
    Scenario: 独立运维入口
      Given 私有配置与明确的目标数据库
      When 运行 aiops-admin 的只读检查或 dry-run
      Then 使用所选配置并报告结果，不创建诊断工作区
