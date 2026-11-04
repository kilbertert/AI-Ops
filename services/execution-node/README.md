# Java 执行节点

本目录包含正式Java节点的Maven定义、源码和资源。它执行已授权的单订单任务，工具清单、
执行清单和完成通知仍由原实现验证；不含Python诊断执行器。

- 构建：`mvn -f services/execution-node/pom.xml -DskipTests=false package`（JDK21，依赖须预置）。
- 产物：`services/execution-node/target/aiops-execution-node.jar`；不提交构建输出。
- 隔离整链与工具验收：[tools/experiments/m2_close/m3node](../../tools/experiments/m2_close/m3node/README.md)。
- 运行与部署：[正式节点手册](../../deploy/node-runtime/README.md)。

本次只移动源码位置；Java包名、Maven坐标、jar文件名、配置字段及宿主安装目录保持原契约。
隔离验收仍含合成模型/CPMS，不能替代真实业务验收。

## 管家统一模型循环

`operator-agent/1`使用已发布业务SOP和工具集合，不按用户关键词切换运行类型。
`gateway-conversation/1`的消息只从Gateway领取；原平台历史/记忆仍拒绝。
节点在每次最终模型请求核对消息前缀、系统提示、工具和模型配置，最多12次模型调用。
金额/过程核验作为受控工具结果返回模型；最终答复引用数据，Gateway完成授权与交付检查。
此协议需要节点、声明、Gateway和Dify应用一起迁移，不能只换提示词。

真实Java/Gateway合成验证使用
`tools/experiments/m2_close/m3node/verify_operator_cpms.py`，含同单追问和切换站点。
历史清单仍可由节点解析用于回退；新Gateway不会自动以旧清单启动新管家运行。
