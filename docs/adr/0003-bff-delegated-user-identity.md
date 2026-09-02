# BFF 委托小程序用户身份

Status: Accepted

第一版由业务后端验证小程序 `thirdSession`，创建一次性不透明身份委托句柄，AI-Ops 使用独立服务身份回查并解析当前用户、租户与订单授权范围。现有设备令牌和内部 HMAC 不能代表用户，裸 `user_id` 或 `tenant_id` 也不能作为授权依据；平台授权服务器未来支持 OAuth 2.0 Token Exchange 后，再迁移为限定 AI-Ops audience、最小权限和短有效期的标准访问令牌。
