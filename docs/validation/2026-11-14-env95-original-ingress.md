# 95原入口回调验收

```gherkin
Feature: Dify回调抵达95自己的Gateway
  Rule: 一次性凭据不被App服务身份覆盖
    Scenario: 已发布客户端请求数据
      Given 既有API域名的精确回调片段指向95回环
      When Dify以一次性Authorization POST已声明回调
      Then 凭据原样抵达Gateway且业务身份头为空
    Scenario: 未允许的方法
      Given 同一回调入口
      When 以错误方法请求
      Then 返回405且不调用Gateway
```

本地复用现役回调/原入口检查，渲染后验证精确路径、回环目标和凭据语义。真实95先备份原vhost/rewrite，
只增加精确回调include，nginx -t后reload；无凭据拒绝，后续客服模型真实调用取证。恢复删除本轮include并恢复备份。
不得把代理拒绝或合成调用写成客户业务通过，未完成业务验收。
