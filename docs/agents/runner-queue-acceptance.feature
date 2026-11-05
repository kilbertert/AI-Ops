Feature: 部署不被失效的 PR agent 阻塞
  Rule: 部署使用独立执行槽
    Scenario: 普通 agent 槽已满
      Given 普通 agent runner 正执行合成任务
      When 派发专用部署探针
      Then 探针在不同 runner 上执行并与合成任务时间重叠

  Rule: 关闭 PR 后只取消对应失效计算
    Scenario: 排队后 PR 被关闭
      Given 白名单 PR agent 正排队且未调用模型
      When 该 PR 被关闭
      Then 对应运行结束且不调用模型
      And 其他 PR 与部署运行不受影响

    Scenario: 延迟关闭事件遇到重新打开
      Given 关闭事件已排队而 PR 已重新打开
      When 清理流程重新读取 PR
      Then 不取消该 PR 的新运行

    Scenario: 任务完成前 head 已变化
      Given agent 的记录 head 已被新提交替代
      When agent 尝试交付
      Then 不推送过期结果

  Rule: 取消释放资源
    Scenario: 运行中的合成容器任务被取消
      Given 同一 SDK 已启动合成 agent 容器
      When 进程收到 SIGINT
      Then 进程退出且本任务容器被移除
