# Java 执行节点

本目录包含正式Java节点的Maven定义、源码和资源。它执行已授权的单订单任务，工具清单、
执行清单和完成通知仍由原实现验证；不含Python诊断执行器。

- 构建：`mvn -f services/execution-node/pom.xml -DskipTests=false package`（JDK21，依赖须预置）。
- 产物：`services/execution-node/target/aiops-execution-node.jar`；不提交构建输出。
- 隔离整链与工具验收：[tools/m2_close/m3node](../../tools/m2_close/m3node/README.md)。
- 运行与部署：[正式节点手册](../../deploy/node-runtime/README.md)。

本次只移动源码位置；Java包名、Maven坐标、jar文件名、配置字段及宿主安装目录保持原契约。
隔离验收仍含合成模型/CPMS，不能替代真实业务验收。
