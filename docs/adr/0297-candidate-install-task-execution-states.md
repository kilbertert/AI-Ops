# 297. 候选安装状态不复用原子任务领取队列

状态：已采纳；修正D37安装任务状态可见性与兼容归属。

## 决定

候选安装账本复用environment_tasks数据库，但有独立领取合同。旧site worker仅定义queued/running/reconcile三种可领取状态，不增加executing/checking枚举到它的SQL集合。公开任务映射独立实现任务对待用户展示的状态。

否则安装态若直接存queued/running/reconcile，会被旧worker误认为现场预检并返回不匹配字段；把“执行中”对外称排队也会向管理员隐藏在飞切换。D37安装态由新claim专属领取，内部waiting/executing/checking分别投影queued/running/reconcile，旧领取SQL排除candidate-install，跨类型lease互斥同时查询正在运行的安装/原子任务。

状态归一继续沿用已采纳ADR0296的操作意图与只读对账；此记录专注共享表的领取边界，不扩展状态机、业务动作或安装语义。原子任务表与现场索引保持兼容。

## 被否决的替代形态

让旧worker领取新增持久态会把安装误派现场检查。改造旧SQL接管全部状态，则旧/新worker混合部署时会双重解释安装任务。另建新表只为避免一个不同claim状态会重复租约、取消和恢复合同，本片以专用安装claim和明确排除更小。

## 后果

旧worker不认识候选安装请求，专用安装worker不会和原子任务同时持环境运行锁。公共状态只来自其对应内存态投影。跨租约占用为合并后新检查，部署顺序仍控制Gateway→新worker；未配安装引用时旧预检/候选准备不变。
