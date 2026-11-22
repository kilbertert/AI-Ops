# 302. 正式跨环境知识能力绑定应用与既有授权世代

Status: Accepted（2026-11-22，用户批准多环境主线，实施拆分由主会话确认）。

## 决定

共享知识仍由中央维护。目标客服通过固定中央HTTPS入口消费精确的
`environment/tenant/workspace/app/library`读能力，不接收共享KB或Console管理密钥。
原KnowledgeGrants是唯一对客授权真值，凭据绑定其当前granted_at；撤权后重授权不能复活旧凭据。
每次目录、检索与媒体交付重核授权及应用归属，目标沿原客服选库、检索和媒体链使用该能力。
私有配置只携带精确应用凭据，配置后缺应用能力即拒绝，不回退到共享无限制读面。

复用Dify External Knowledge API的Bearer、knowledge_id/query/retrieval_setting与records/metadata合同，
目标客户端消费同一读入口；媒体由中央限定搜到的对象，再复用目标原QA短时签名URL与读取前后重核，不绕过本地准入和路由撤回。运维签发只导出现有授权，
不能自动grant、注册应用、发布或激活。原ckx候选和ckc目录凭据保持原语义。

## 标准与取舍

[RFC6750§2.1/§5](https://www.rfc-editor.org/rfc/rfc6750)：凭据只走Authorization: Bearer，远端HTTPS、禁止重定向带出凭据；
其是资源Bearer用法，不声称实现OAuth授权服务器。Dify External Knowledge API定义检索请求与
records，metadata可携来源与媒体；本仓现有dify_knowledge_api解析/呈现为唯一协议适配。
HMAC能力沿现有候选实现的密钥隔离域，但正式能力使用独立版本前缀及应用/授权世代约束。
原共享key是不透明字符串，精确匹配先于新前缀分流，不能为新能力禁止旧key的前缀。
[RFC9110§14](https://www.rfc-editor.org/rfc/rfc9110#section-14)的字节范围在目标原MediaProxy处理一次；
中央读完整媒体，目标按原范围响应，避免中央206切片被目标再切片。委派媒体读取上限32MiB，超限明确不可用。

## 备选方案

复制全局KB读取面可免代理联动，但会让目标伪造其他tenant header；把候选token改名会沿用过期任务而非正式授权；
新建授权表会与KnowledgeGrants漂移，均不采用。现有不配置委派的直连部署不改变前提。

## 后果

目标新增中央知识依赖；中央不可用则明确知识不可用，不能切回无限读面。
本片是可部署知识依赖，不替代P4正式发布/安装账本准入恢复及P5业务验收。
