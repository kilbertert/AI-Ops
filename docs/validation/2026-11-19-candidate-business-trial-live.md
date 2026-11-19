# 124 停用管家候选试运行回执

本事件接续[#1609运行资源登记](2026-11-19-runtime-registration-live.md)及[#1612候选试运行实现](2026-11-19-runtime-install-live.md)。
配置已部署、候选业务试运行失败；不表示业务验收、环境启用或租户开通。

## 配置事务

2026-11-19，在已登记目标124以固定安装器`040ffa9af7587730a2949be0240ca5804aac7cf8`执行同修订配置事务。
应用版本保持`1.31.0+040ffa9af758`，没有代码升级；为目标Gateway配置新增独立
`AIOPS_GATEWAY_ENVIRONMENT_TASK_KEY`，供本机受限HMAC候选入口使用。配置通过原生产配置、停用目录和运行工件的
expected-digest事务成套校验，不修改公司服务、公司业务库、正式路由或Dify应用发布状态。

配置事务最终安装成功，Gateway由PID`1866147`切换为`3424299`，健康接口返回原版本。
安装账本无pending；目录revision仍为3，环境和两个租户均停用，管理授权为空。
业务库、运行工件、正式Dify注册表及公司容器身份保持。部署后安装器现场预检8项本地passed，
受管主机身份、正式网络与业务3项仍pending，`executable=false`。

匿名POST候选入口返回401；这只证明入口需要HMAC，不证明候选业务可运行。

## 正常身份与真实试跑

使用受控资产`aiops-124-test-admin-phone-20261116`，在124目标UPMS正常登录并通过同一服务的`/user/info`
核对tenant=`1`、user type=`1`。登录令牌仅在远端进程内存使用，没有写入回执或日志。
使用已登记管家应用、当前runtime摘要及独立本机HMAC提交一轮只读自然语言站点咨询。

候选入口接受任务并返回试运行编号；最终状态为`failed`，错误码`OPERATOR_DEPENDENCY_UNAVAILABLE`。
Gateway的实际run原因是`operator_node_failed: model_failed`；节点未回报实际node id、Agent版本或工具摘要，
`exec_config_state=expected`，未取得业务答复。没有重试或改用其他环境/应用掩盖失败。

因此，本次证明正常目标登录、HMAC边界、受限候选入口、真实任务提交及失败归因链路工作；
未证明124模型、节点到模型代理的链路、CPMS工具往返或任何业务答案。环境与租户仍停用、
候选runtime仍标记`installed=false/activated=false`，没有正式路由、grant或应用key变更。

## 现场状态与恢复

- Gateway：`1.31.0+040ffa9af758`，PID`3424299`，health通过。
- 当前配置摘要：`11a449954cef52ce9721dcf9a7ec86a4561548494f2540ca1e831207239f4cc5`；旧备份由安装器保留。
- 环境目录revision 3；环境及两个租户均关闭，管理授权为空。
- 运行工件摘要不变；schema、正式路由、现役应用和业务数据保持。
- 失败的trial/run保留在124自有数据库中，作为本次运行证据；未删除或伪造成功状态。
- 原始账号、HMAC key、令牌、问题/响应正文和主机快照仅存受控私有记录，不进入仓库。

安装回退可通过同版固定安装器恢复原配置与原制品；本次没有执行回退。恢复配置会让新HMAC入口重新关闭。
这不是业务节点故障恢复或业务验收。

后续应调查`model_failed`对应的目标节点及模型依赖，使用同一正常身份和停用候选路径复验；
在取得实际node/build、模型调用结果和至少一个真实只读工具往返前，不得激活124环境或租户。

原始审计、账号与凭据只存私有操作记录：
`/home/claude/.cache/aiops-candidate-business-trial-20261119/`。
