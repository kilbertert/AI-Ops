Feature: 管家完成拒绝可定位
  Scenario: 无效答案被拒绝
    Given 节点的答案不符合现有交付合同
    When Gateway拒绝完成通知
    Then 原HTTP拒绝及作业失败行为保持不变
    And 服务日志记录固定拒绝原因码
    And 不记录令牌、订单号或模型正文
