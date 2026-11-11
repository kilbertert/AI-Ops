# 设备离线分析实施

用户确认方案，分支feat/device-offline-analysis，初始基线649bff0a。
复用offline卡和管家Agent，新增有界离线事实、区间并集和订单关联；详细合同见
[场景](../acceptance/device-offline.feature)与[QA计划](../acceptance/device-offline-qa-plan.md)。
已只读核对41两租户存在离线记录，DATETIME及数据库会话+08:00已核；业务容器TZ=Asia/Shanghai已只读核对。
已实现共用读面、offline卡、管家工具与回读授权；固定6b44d640全量4746通过/9跳过，
真实MySQL隔离验证、Java/Gateway三轮及一次性Gateway均通过。独立反例修复中，生产仍未部署。
下一步：闭合独立/Devin复核，最终CI合并后执行已批准索引、声明/Gateway配套部署与原页面验收。
