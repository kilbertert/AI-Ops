Feature: 已发布 SOP 经过真实调度平台交给打包节点
  Rule: 真实请求之前检查身份依赖
    Scenario: 只有公司身份接口不可达
      Given Redis 和执行平台的健康检查通过
      And Gateway 容器不能访问其配置的 C 到 B 身份接口
      When 运行正式 deps 检查
      Then 检查失败且不得据此开始业务请求
  Rule: 编辑与运行使用同一发布纪律
    Scenario: 保存、试用、发布和恢复
      Given 隔离真实 Snail 平台通过心跳登记了真实 Java 节点
      And 模型、编辑器、订单和身份是明确标记的合成来源
      When 通过原 API 发布 V1、保存 V2、试用 V2、发布 V2、撤回、恢复 V1
      Then 保存与试用不改变 App 执行 V1
      And 发布后 App 执行 V2 且节点确认同一清单摘要
      And 撤回后新请求和历史正文返回 410
      And 恢复后 App 再执行 V1
    Scenario: 模型收尾失败
      Given 同一真实调度链已经可用
      When 模型工具循环第二轮返回 500
      Then QA 失败且没有正文
      And 不回落直读或另一执行引擎
