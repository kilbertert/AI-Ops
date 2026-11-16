# 测试主体发现与共享服务HTTPS恢复

基线`7e59938f`，分支`feat/shared-dify-https-fallback`，承接#1536。
用户授权自行寻找目标测试账号并临时恢复网络，保留不改公司仓库/服务与不用专属SSH业务隧道的限制。

已完成：现有测试租户账号经原UPMS验证码登录，再经公网公司网关读取身份，用户/租户一致；资产已加密归档。
没有建账号、改密码/权限或写入Redis会话。该步骤仍不证明原App或Dify登录页面已通过。
原HTTPS多次握手失败；边缘代理试验出现403/525，DNS和新增规则已完整恢复。

已准备受限备用HTTPS配置、合并制品构建、带备份安装/恢复入口和真实Nginx验证。
仅固定服务API，来源白名单＋Dify原Bearer，管理端点拒绝；实际部署和模型流式验证待完成。
详见[操作手册](../agents/shared-dify-https-fallback.md)及[回执](../validation/2026-11-16-shared-dify-https-fallback.md)。
**未完成业务验收**；P0仍需业务工具往返与容量证据。
