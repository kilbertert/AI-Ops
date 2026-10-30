# 节点工具声明部署缺口

基线`20cbc1db`，分支`fix/node-tools-deployment`。目录验收发现原管家H5正常登录后202→failed，
同QA唯一run为operator_node_failed、expected、无快照。节点日志明确“找不到工具声明: operator_tools.json”。
只读核实生产没有声明文件、加载参数或挂载；现役Compose也未配置，文档要求未落到部署资产。

NT-01新增回归先红（JVM参数缺失），修复后部署专项3 passed；NT-02锁定JDK真实两侧解析PASS tools=3。
修复只给node增加JVM属性与只读bind，create_host_path=false拒绝缺失文件；不改变Java、网关权限或工具范围。
CI、NT-03部署与NT-04原H5复验待完成，未完成业务验收。原始失败日志与登录状态只留私有目录。
