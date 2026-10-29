Feature: 管家各入口使用相同的结构化诊断输出
  Rule: 输出协议由执行系统负责，业务提示词只描述诊断方法
    Scenario: 自由书写的业务SOP仍能交付报告
      Given Dify草稿只有业务SOP且启用了两项CPMS工具
      When 从正式入口或Dify试运行执行同一订单诊断
      Then 每轮模型请求都明确指定JSON对象输出
      And 原入口收到通过六字段和证据校验的可读报告
      And 试运行不改变正式发布指针

    Scenario: 不同提问得到不同报告
      Given 同一张已授权订单
      When 分别询问该订单的电费与充电电压
      Then 两份报告的 direct_answer 各不相同
      And 报告正文以 direct_answer 开头
      And 金额字段与额定功率出现在本次证据里

    Scenario: 工具调用前的解释不是最终报告
      Given 模型在调用工具时输出了进度文字
      When 最后一轮输出有效的诊断JSON
      Then Gateway只校验最后一轮完整回答
      And 进度文字不混入诊断JSON

  Rule: 输出约束不替代证据和安全校验
    Scenario Outline: 无效结果不能显示为诊断成功
      Given 节点已读取本次授权订单的证据
      When 模型返回<结果>
      Then 原入口显示失败且没有诊断正文
      Examples:
        | 结果 |
        | 供应商错误 |
        | 被截断的JSON |
        | 无视JSON模式的自由文本 |
        | 字段缺失的JSON |
        | 未交付工具的证据声明 |
        | 已执行退款的虚假声明 |
        | 空的 direct_answer（未回答提问） |
        | 空正文 |
        | 超过24000字符的正文 |
