# 运维工具凭据代表租户服务

独立 key 的摘要绑定环境、租户、workspace、app 与 agent；每次调用重读私有绑定并核对现行登记。
该服务按租户读取，不伪装真人或扩大存量 consumer/self、operator 站点权限。
Bearer key 证明所登记能力，不证明实际调用 app/人来源；审计字段因此明确叫 `registered_*`。
按 RFC 6750 的持有语义保护传输和存储，不将本实现宣称为完整 OAuth。
后续 App 接 Dify 必须另证可信个人范围；本片不引入 range token。
