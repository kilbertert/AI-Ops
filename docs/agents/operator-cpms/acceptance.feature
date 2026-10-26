Feature: 管家异常订单使用 CPMS 正式接口取证
  新工具只读 CPMS API，不调用旧 AI-Ops 诊断工具或直连业务库。

  Rule: 工具读取受当前任务和管家范围约束
    Scenario: 读取授权订单详情和有限监测样本
      Given 当前管家有权管理该订单站点且正式版本允许两个工具
      When Java 节点调用订单详情和充电监测工具
      Then CPMS 收到固定 GET 路径和该次授权凭据
      And 返回值只包含本订单诊断字段与来源时间
      And 监测数据标明分页截断及排序未保证
      And 模型不能获得登录凭据、手机号或任意额外字段

    Scenario: 输入不能改变取数目标
      Given 当前任务已固定订单及允许工具
      When 工具参数含订单号、租户、URL或任意非空参数
      Then 在请求 CPMS 前拒绝

    Scenario: CPMS 返回了其他订单或站点
      Given 当前任务只允许指定租户及站点内的一张订单
      When CPMS 响应的订单、租户或站点不符
      Then 不向模型交付任何业务正文

    Scenario: 读取过程中撤权或到期
      Given CPMS 请求已经发出但响应尚未交付
      When 当前任务授权撤回、变更或过期
      Then 后续交付被拒绝
      And 不声称追回此前已发出的 CPMS 请求

  Rule: 外部失败不成为诊断成功
    Scenario Outline: 不可信上游响应
      Given 当前任务有有效授权
      When CPMS 返回 <response>
      Then 工具返回明确失败且不回落旧工具或数据库
      Examples:
        | response |
        | 重定向 |
        | HTTP错误 |
        | 业务错误信封 |
        | 非JSON或过大的正文 |
        | 缺少归属字段 |

  Rule: 发布控制必须改变实际节点行为
    Scenario: 关闭并发布工具
      Given 工具在旧正式版本可用
      When 运营关闭该工具并发布新版本
      Then 新任务不提供该工具
      And 强行调用被拒且不产生CPMS请求
      And 全部关闭不恢复任何默认工具

    Scenario: 完整管家业务交付
      Given 真人从公司管家端正常登录并选择有权限的异常订单
      When 经已发布SOP提交一次诊断
      Then 原页面呈现事实、证据、可能原因和检查建议
      And 真实工程师可对照本次证据核对结论
