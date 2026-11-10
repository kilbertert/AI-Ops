# 设备离线分析实施

用户确认方案，分支feat/device-offline-analysis，初始基线649bff0a。
复用offline卡和管家Agent，新增有界离线事实、区间并集和订单关联；详细合同见
[场景](../acceptance/device-offline.feature)与[QA计划](../acceptance/device-offline-qa-plan.md)。
已只读核对41两租户存在离线记录，DATETIME及数据库会话+08:00已核；业务容器TZ=Asia/Shanghai已只读核对。
当前实施中，代码/真实链路验证与上线尚未完成，不能把数据存在记成功能通过。
