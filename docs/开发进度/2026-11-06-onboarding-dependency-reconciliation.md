# 一键开通依赖收敛

目标：解决应用升级后开通依赖漂移，避免逐租户手工同步。分支fix/onboarding-dependency-reconciliation，
基线e6aed1ed；[决定](../adr/0235-onboarding-dependencies-follow-authorities.md)。
已完成来源插件版本唯一化、受管provider收敛/回读、重试复核、明确错误、调用方与夹具更新。
实际故障回归先红后绿；独立Hack发现误覆盖自定义参数，已据反例收紧，已通过9项独立复验及2项入口重放。
验证与限制见[本轮记录](../validation/2026-11-06-onboarding-dependency-reconciliation.md)。
用户已授权完整交付。下一步：一个PR、必需CI、合并、配套部署与原任务重试；版本1.2.6。
未修改生产配置、未安装生产插件、未点击原任务重试，不以代码完成宣称真实开通已恢复。
