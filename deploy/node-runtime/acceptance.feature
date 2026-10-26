Feature: 已发布 SOP 的正式节点执行
  Rule: 正式运行沿用现有 Gateway 与用户接口
    Scenario: 正式启动及页面交付
      Given 节点制品摘要一致且模型出口为批准的供应商
      When 启动持久服务并从运营发布入口发布 SOP
      And 本人通过真实 H5 查询订单状态
      Then 原 POST 接单且原 GET 返回同一 run 的快照
      And 助手区域展示订单状态、来源与时间
    Scenario: 内部回调与个人范围
      Given 节点仅连接内部网络
      When 请求非回调路径或读取他人订单
      Then 请求被拒且没有业务正文
    Scenario: 回调身份由TLS证书验证
      Given 节点已信任项目CA且证书SAN匹配内部回调地址
      When 节点通过HTTPS读取授权和提交证据
      Then 原凭据和正文被送往Gateway
      And 未信任证书或明文HTTP不能取得业务响应
  Rule: 退役保留恢复能力
    Scenario: 新链通过后退役试验配置
      Given 真实验收通过且已核对旧路径的运行引用
      When 归档本项目旧试验配置
      Then 正式服务保持启用且历史证据与恢复材料仍可读取
