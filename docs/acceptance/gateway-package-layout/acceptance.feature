Feature: Gateway和管家会话归位
  Scenario: 服务入口稳定
    Given 同版安装的Gateway
    When 执行aiops-gateway serve并调用公开接口
    Then 原鉴权和响应契约保持一致且旧诊断路径仍返回404
  Scenario: 知识页面随包交付
    Given 干净wheel与镜像
    When 访问知识页面
    Then 页面资源可从knowledge包加载
